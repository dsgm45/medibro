"""
Tests for the AI Health Chat's safety properties.

Same testing philosophy as test_symptom_checker.py: a fake Gemini client,
no real network calls, fully deterministic. The single most important
property: the AI is NEVER consulted when a message contains crisis
language (physical emergency or mental health crisis indicators) - that
check is deterministic and runs before the AI is ever reached.
"""
import app as app_module
from conftest import login


class _FakeFinishReason:
    def __init__(self, name):
        self.name = name


class _FakeCandidate:
    def __init__(self, finish_reason_name):
        self.finish_reason = _FakeFinishReason(finish_reason_name)


class _FakeGeminiResponse:
    def __init__(self, text, finish_reason_name='STOP'):
        self.text = text
        self.candidates = [_FakeCandidate(finish_reason_name)]


class _FakeModels:
    def __init__(self, response=None, exception=None):
        self._response = response
        self._exception = exception
        self.call_count = 0
        self.last_kwargs = None

    def generate_content(self, **kwargs):
        self.call_count += 1
        self.last_kwargs = kwargs
        if self._exception:
            raise self._exception
        return self._response


class _FakeGeminiClient:
    def __init__(self, response=None, exception=None):
        self.models = _FakeModels(response=response, exception=exception)


def _send_chat_message(client, message):
    return client.post('/ai-chat', data={'message': message}, follow_redirects=True)


def _all_chat_messages(patient_id):
    return (
        app_module.AIChatMessage.query
        .filter_by(patient_id=patient_id)
        .order_by(app_module.AIChatMessage.created_at.asc())
        .all()
    )


class TestCrisisNeverCallsAI:
    def test_physical_emergency_language_skips_ai(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('This would have been an AI reply.'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        _send_chat_message(client, "I'm having chest pain and can't breathe")

        assert fake_client.models.call_count == 0
        messages = _all_chat_messages(patient_id)
        assert len(messages) == 2  # patient message + crisis reply
        assert messages[0].sender == 'patient'
        assert messages[1].sender == 'ai'
        assert messages[1].is_crisis_response is True
        assert 'emergency' in messages[1].content.lower()

    def test_mental_health_crisis_language_skips_ai(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('This would have been an AI reply.'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        _send_chat_message(client, 'I want to kill myself')

        assert fake_client.models.call_count == 0
        messages = _all_chat_messages(patient_id)
        assert messages[1].is_crisis_response is True
        assert '988' in messages[1].content  # crisis line number present

    def test_ordinary_health_question_does_not_trigger_crisis_path(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        good_text = 'Staying hydrated and eating balanced meals generally supports heart health.'
        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse(good_text, finish_reason_name='STOP'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        _send_chat_message(client, 'What foods are good for heart health?')

        assert fake_client.models.call_count == 1  # AI WAS consulted this time
        messages = _all_chat_messages(patient_id)
        assert messages[1].is_crisis_response is False
        assert messages[1].content == good_text


class TestAIUnavailableFallback:
    def test_no_client_configured_shows_fallback_message(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = _send_chat_message(client, 'What foods are good for heart health?')

        assert resp.status_code == 200
        messages = _all_chat_messages(patient_id)
        assert len(messages) == 2
        assert messages[1].is_crisis_response is False
        assert messages[1].content  # got the fallback message, not empty


class TestTruncationAndMalformationSafety:
    def test_non_stop_finish_reason_falls_back(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        truncated = _FakeGeminiResponse('For general wellness, focus', finish_reason_name='MAX_TOKENS')
        fake_client = _FakeGeminiClient(response=truncated)
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        _send_chat_message(client, 'Any general wellness tips?')

        messages = _all_chat_messages(patient_id)
        assert messages[1].content != 'For general wellness, focus'

    def test_markdown_artifact_response_falls_back(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        garbled = _FakeGeminiResponse('**Content:** * *', finish_reason_name='STOP')
        fake_client = _FakeGeminiClient(response=garbled)
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        _send_chat_message(client, 'Any general wellness tips?')

        messages = _all_chat_messages(patient_id)
        assert '**' not in messages[1].content

    def test_api_exception_falls_back_without_crashing(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        fake_client = _FakeGeminiClient(exception=RuntimeError('simulated API failure'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        resp = _send_chat_message(client, 'Any general wellness tips?')

        assert resp.status_code == 200
        messages = _all_chat_messages(patient_id)
        assert len(messages) == 2
        assert messages[1].content


class TestConversationHistory:
    def test_prior_messages_are_included_as_context_without_duplication(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('First reply here.', 'STOP'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)
        _send_chat_message(client, 'What foods are good for heart health?')

        # Second message - fake client's response changes, but what matters
        # is that the SECOND call's history includes the first exchange.
        fake_client.models._response = _FakeGeminiResponse('Second reply here.', 'STOP')
        _send_chat_message(client, 'What about exercise?')

        assert fake_client.models.call_count == 2
        second_call_contents = fake_client.models.last_kwargs['contents']
        # 2 prior turns (patient + ai from first exchange) + 1 new patient turn = 3
        assert len(second_call_contents) == 3
        # The new message should appear exactly once, not duplicated
        new_message_occurrences = sum(
            1 for c in second_call_contents
            if any(p.get('text') == 'What about exercise?' for p in c.parts)
        )
        assert new_message_occurrences == 1

        messages = _all_chat_messages(patient_id)
        assert len(messages) == 4  # 2 patient + 2 ai across both exchanges

"""
Tests for the personalized AI context feature: build_patient_context_summary()
and its integration into both AI functions (symptom checker + chat).

Uses the same fake Gemini client pattern as the other AI test files - no
real network calls, fully deterministic.
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
    def __init__(self, response=None):
        self._response = response
        self.call_count = 0
        self.last_kwargs = None

    def generate_content(self, **kwargs):
        self.call_count += 1
        self.last_kwargs = kwargs
        return self._response


class _FakeGeminiClient:
    def __init__(self, response=None):
        self.models = _FakeModels(response=response)


class TestBuildPatientContextSummary:
    def test_no_data_returns_empty_string(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            result = app_module.build_patient_context_summary(patient_id)
        assert result == ''

    def test_includes_vitals_when_present(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            vital = app_module.Vital(
                patient_id=patient_id, systolic=120, diastolic=80,
                heart_rate=72, spo2=98, temperature=98.6
            )
            app_module.db.session.add(vital)
            app_module.db.session.commit()

            result = app_module.build_patient_context_summary(patient_id)
        assert 'BP 120/80' in result
        assert 'HR 72 bpm' in result
        assert 'reference only' in result

    def test_includes_active_medicines(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Metformin')
            app_module.db.session.add(med)
            app_module.db.session.commit()

            result = app_module.build_patient_context_summary(patient_id)
        assert 'Metformin' in result

    def test_excludes_expired_medicine(self, client, make_user):
        import datetime as dt
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            expired_med = app_module.Medicine(
                patient_id=patient_id, name='OldMedicine',
                start_date=dt.date(2020, 1, 1), end_date=dt.date(2020, 2, 1)
            )
            app_module.db.session.add(expired_med)
            app_module.db.session.commit()

            result = app_module.build_patient_context_summary(patient_id)
        assert 'OldMedicine' not in result
        assert result == ''

    def test_includes_recent_symptoms(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            log = app_module.SymptomLog(
                patient_id=patient_id, symptoms='Headache', severity='mild', guidance='x'
            )
            app_module.db.session.add(log)
            app_module.db.session.commit()

            result = app_module.build_patient_context_summary(patient_id)
        assert 'Headache' in result
        assert 'mild' in result


class TestSymptomCheckerUsesContext:
    def test_context_included_when_patient_has_data(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        with app_module.app.app_context():
            vital = app_module.Vital(patient_id=patient_id, heart_rate=90)
            app_module.db.session.add(vital)
            app_module.db.session.commit()

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('Rest and monitor your symptoms.', 'STOP'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        client.post('/symptoms', data={'symptoms': 'Fatigue', 'severity': 'mild', 'description': ''}, follow_redirects=True)

        assert fake_client.models.call_count == 1
        sent_prompt = fake_client.models.last_kwargs['contents']
        assert 'HR 90 bpm' in sent_prompt
        assert 'Fatigue' in sent_prompt

    def test_no_context_block_when_patient_has_no_data(self, client, make_user, monkeypatch):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('Rest and monitor your symptoms.', 'STOP'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        client.post('/symptoms', data={'symptoms': 'Fatigue', 'severity': 'mild', 'description': ''}, follow_redirects=True)

        sent_prompt = fake_client.models.last_kwargs['contents']
        assert not sent_prompt.startswith('\n')
        assert 'Patient context' not in sent_prompt
        assert sent_prompt.startswith('Symptoms:')


class TestChatUsesContext:
    def test_context_included_in_chat_prompt(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Lisinopril')
            app_module.db.session.add(med)
            app_module.db.session.commit()

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('That sounds manageable with rest.', 'STOP'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        client.post('/ai-chat', data={'message': 'Is it okay to exercise today?'}, follow_redirects=True)

        assert fake_client.models.call_count == 1
        sent_prompt = fake_client.models.last_kwargs['contents']
        assert 'Lisinopril' in sent_prompt
        assert 'Is it okay to exercise today?' in sent_prompt

    def test_matches_proven_shape_when_no_context_or_history(self, client, make_user, monkeypatch):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('Sure, that sounds fine.', 'STOP'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        client.post('/ai-chat', data={'message': 'What foods are good for heart health?'}, follow_redirects=True)

        sent_prompt = fake_client.models.last_kwargs['contents']
        # With no patient data and no history, this should exactly match the
        # bare message - the same proven-working shape from before.
        assert sent_prompt == 'What foods are good for heart health?'

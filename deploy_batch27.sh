#!/bin/bash
set -e

echo "=== MediBro: Add tests for AI symptom checker safety behavior ==="

if [ ! -f "app.py" ]; then
  echo "ERROR: app.py not found. cd into your medimind project folder first, then re-run this script."
  exit 1
fi

mkdir -p tests

cat > tests/test_symptom_checker.py << 'TEST_EOF'
"""
Tests for the AI-assisted symptom checker's safety properties.

These tests use a fake Gemini client rather than calling the real API -
no network access needed, and the tests are fully deterministic. The fake
client mimics just enough of the real google-genai response shape
(.text, .candidates[0].finish_reason.name) for get_ai_symptom_guidance()
to work with it exactly as it would with a real response.

The single most important property tested here: the AI is NEVER consulted
for emergency-level cases, even when a working AI client is configured.
That's what makes the emergency path safe to depend on regardless of the
AI integration's availability or correctness.
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
    """Counts calls so tests can assert the AI was (or wasn't) invoked."""
    def __init__(self, response=None, exception=None):
        self._response = response
        self._exception = exception
        self.call_count = 0

    def generate_content(self, **kwargs):
        self.call_count += 1
        if self._exception:
            raise self._exception
        return self._response


class _FakeGeminiClient:
    def __init__(self, response=None, exception=None):
        self.models = _FakeModels(response=response, exception=exception)


def _submit_symptoms(client, symptoms=None, severity='mild', description=''):
    data = {'severity': severity, 'description': description}
    return client.post('/symptoms', data=[
        *[('symptoms', s) for s in (symptoms or [])],
        *data.items(),
    ], follow_redirects=True)


def _latest_symptom_log(patient_id):
    return (
        app_module.SymptomLog.query
        .filter_by(patient_id=patient_id)
        .order_by(app_module.SymptomLog.created_at.desc())
        .first()
    )


class TestEmergencyNeverCallsAI:
    """The single most important safety property in this feature."""

    def test_emergency_symptom_skips_ai_entirely(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('This would have been AI guidance.'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        _submit_symptoms(client, symptoms=['Chest pain'], severity='mild')

        assert fake_client.models.call_count == 0  # AI was never invoked
        log = _latest_symptom_log(patient_id)
        assert log is not None
        assert log.ai_generated is False
        assert 'emergency' in log.guidance.lower() or 'seek' in log.guidance.lower()

    def test_severe_severity_skips_ai_regardless_of_symptoms(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('This would have been AI guidance.'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        _submit_symptoms(client, symptoms=['Fatigue'], severity='severe')

        assert fake_client.models.call_count == 0
        log = _latest_symptom_log(patient_id)
        assert log.ai_generated is False


class TestAIUnavailableFallback:
    def test_no_client_configured_uses_rule_based_guidance(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        # gemini_client is None by default in the test environment (no
        # GEMINI_API_KEY set) - this is the real, common case in dev/test.
        _submit_symptoms(client, symptoms=['Fever'], severity='mild')

        log = _latest_symptom_log(patient_id)
        assert log is not None
        assert log.ai_generated is False
        assert log.guidance  # got some guidance, just not AI-generated


class TestAISuccess:
    def test_clean_complete_response_is_used_and_marked_ai_generated(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        good_text = 'Rest and stay hydrated. See a doctor if this persists beyond a few days.'
        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse(good_text, finish_reason_name='STOP'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        _submit_symptoms(client, symptoms=['Fever'], severity='mild')

        assert fake_client.models.call_count == 1
        log = _latest_symptom_log(patient_id)
        assert log.ai_generated is True
        assert log.guidance == good_text


class TestTruncationAndMalformationSafety:
    """Regression tests for the exact bug class found in manual testing:
    Gemini's thinking tokens ate the output budget, producing responses
    cut off mid-sentence or containing stray markdown fragments."""

    def test_non_stop_finish_reason_falls_back(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        truncated = _FakeGeminiResponse('For a mild cough, focus', finish_reason_name='MAX_TOKENS')
        fake_client = _FakeGeminiClient(response=truncated)
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        _submit_symptoms(client, symptoms=['Cough'], severity='mild')

        log = _latest_symptom_log(patient_id)
        assert log.ai_generated is False  # rejected due to finish_reason, fell back
        assert log.guidance != 'For a mild cough, focus'

    def test_response_missing_ending_punctuation_falls_back(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        # finish_reason says STOP, but the text itself looks cut off -
        # exactly the shape of the real bug report.
        cut_off = _FakeGeminiResponse('For a mild cough,', finish_reason_name='STOP')
        fake_client = _FakeGeminiClient(response=cut_off)
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        _submit_symptoms(client, symptoms=['Cough'], severity='mild')

        log = _latest_symptom_log(patient_id)
        assert log.ai_generated is False

    def test_markdown_artifact_response_falls_back(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        garbled = _FakeGeminiResponse('ing Content:** * *', finish_reason_name='STOP')
        fake_client = _FakeGeminiClient(response=garbled)
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        _submit_symptoms(client, symptoms=['Cough'], severity='mild')

        log = _latest_symptom_log(patient_id)
        assert log.ai_generated is False

    def test_api_exception_falls_back_without_crashing(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        fake_client = _FakeGeminiClient(exception=RuntimeError('simulated API failure'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        resp = _submit_symptoms(client, symptoms=['Fever'], severity='mild')

        assert resp.status_code == 200  # no 500, handled gracefully
        log = _latest_symptom_log(patient_id)
        assert log.ai_generated is False
        assert log.guidance  # still got the rule-based fallback message
TEST_EOF

echo "Files written."
echo ""
echo "=== Running the full test suite ==="
python3 -m pytest -v

echo ""
echo "=== Test run complete - see results above ==="
echo "These new tests use a fake Gemini client (no real API calls, no"
echo "network needed) - they specifically verify the AI is never consulted"
echo "for emergency symptoms, and that truncated/malformed AI responses"
echo "(the exact bug you found) get correctly caught and rejected."
echo ""
read -p "Press Enter to commit and push, or Ctrl+C to stop here: "

git add tests/test_symptom_checker.py
git commit -m "Add tests for AI symptom checker: emergency-skip safety, AI fallback, and truncation/malformation regression tests"
git push origin main

echo ""
echo "=== Done. Check Render dashboard for the new deploy. ==="
echo "This doesn't change any live behavior - tests only, backing up the"
echo "fix from the last batch."

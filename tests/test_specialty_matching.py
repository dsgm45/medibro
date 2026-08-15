"""
Tests for the symptom-to-specialty matching feature. Reuses the same fake
Gemini client pattern established in test_symptom_checker.py.

Key properties tested: never suggests anything alongside an emergency
symptom, never suggests a specialty with no real bookable doctor, AI
suggestion is validated against real data before being trusted, and the
deterministic mapping is a reliable fallback when AI is unavailable.
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


def _make_doctor_with_specialty(make_user, email, specialty):
    with app_module.app.app_context():
        user_id = make_user(email, 'password123', role='doctor', status='approved')
        doctor = app_module.db.session.get(app_module.User, user_id)
        doctor.specialty = specialty
        app_module.db.session.commit()
    return user_id


class TestDeterministicFallback:
    def test_suggests_matching_specialty_when_real_doctor_exists(self, client, make_user, monkeypatch):
        _make_doctor_with_specialty(make_user, 'derm@example.com', 'Dermatology')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        monkeypatch.setattr(app_module, 'gemini_client', None)  # force deterministic path

        client.post('/symptoms', data={'symptoms': 'Rash', 'severity': 'mild', 'description': ''}, follow_redirects=True)

        with app_module.app.app_context():
            entry = app_module.SymptomLog.query.order_by(app_module.SymptomLog.created_at.desc()).first()
            assert entry.suggested_specialty == 'Dermatology'

    def test_no_suggestion_when_no_matching_doctor_exists(self, client, make_user, monkeypatch):
        _make_doctor_with_specialty(make_user, 'cardio@example.com', 'Cardiology')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        monkeypatch.setattr(app_module, 'gemini_client', None)

        # Rash maps to Dermatology, but only a Cardiology doctor exists
        client.post('/symptoms', data={'symptoms': 'Rash', 'severity': 'mild', 'description': ''}, follow_redirects=True)

        with app_module.app.app_context():
            entry = app_module.SymptomLog.query.order_by(app_module.SymptomLog.created_at.desc()).first()
            assert entry.suggested_specialty is None

    def test_no_suggestion_when_no_doctors_at_all(self, client, make_user, monkeypatch):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        monkeypatch.setattr(app_module, 'gemini_client', None)

        client.post('/symptoms', data={'symptoms': 'Rash', 'severity': 'mild', 'description': ''}, follow_redirects=True)

        with app_module.app.app_context():
            entry = app_module.SymptomLog.query.order_by(app_module.SymptomLog.created_at.desc()).first()
            assert entry.suggested_specialty is None


class TestEmergencySuppression:
    def test_no_suggestion_for_emergency_symptom_even_with_matching_doctor(self, client, make_user, monkeypatch):
        _make_doctor_with_specialty(make_user, 'cardio@example.com', 'Cardiology')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        monkeypatch.setattr(app_module, 'gemini_client', None)

        # Chest pain is an emergency symptom AND maps to Cardiology, which
        # genuinely exists here - the suggestion must still be suppressed.
        client.post('/symptoms', data={'symptoms': 'Chest pain', 'severity': 'mild', 'description': ''}, follow_redirects=True)

        with app_module.app.app_context():
            entry = app_module.SymptomLog.query.order_by(app_module.SymptomLog.created_at.desc()).first()
            assert entry.suggested_specialty is None


class TestAiEnhancement:
    def test_ai_suggestion_used_when_valid(self, client, make_user, monkeypatch):
        _make_doctor_with_specialty(make_user, 'gastro@example.com', 'Gastroenterology')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('Gastroenterology', 'STOP'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        client.post('/symptoms', data={'symptoms': 'Fatigue', 'severity': 'mild', 'description': 'stomach pain after eating'}, follow_redirects=True)

        with app_module.app.app_context():
            entry = app_module.SymptomLog.query.order_by(app_module.SymptomLog.created_at.desc()).first()
            assert entry.suggested_specialty == 'Gastroenterology'

    def test_ai_suggestion_rejected_if_not_a_real_specialty(self, client, make_user, monkeypatch):
        _make_doctor_with_specialty(make_user, 'derm@example.com', 'Dermatology')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        # AI hallucinates a specialty that doesn't actually exist on the platform
        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('Endocrinology', 'STOP'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        client.post('/symptoms', data={'symptoms': 'Fatigue', 'severity': 'mild', 'description': ''}, follow_redirects=True)

        with app_module.app.app_context():
            entry = app_module.SymptomLog.query.order_by(app_module.SymptomLog.created_at.desc()).first()
            # Fatigue maps to General Physician in the fallback, which
            # doesn't exist here either (only Dermatology does) - so the
            # bogus AI answer must be rejected and correctly fall through
            # to no suggestion rather than the invented specialty.
            assert entry.suggested_specialty is None

    def test_ai_says_none_produces_no_suggestion(self, client, make_user, monkeypatch):
        _make_doctor_with_specialty(make_user, 'derm@example.com', 'Dermatology')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('NONE', 'STOP'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        client.post('/symptoms', data={'symptoms': 'Fatigue', 'severity': 'mild', 'description': ''}, follow_redirects=True)

        with app_module.app.app_context():
            entry = app_module.SymptomLog.query.order_by(app_module.SymptomLog.created_at.desc()).first()
            assert entry.suggested_specialty is None

    def test_falls_back_to_deterministic_when_ai_fails(self, client, make_user, monkeypatch):
        _make_doctor_with_specialty(make_user, 'derm@example.com', 'Dermatology')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        # AI response with a bad finish_reason - triggers the fallback path
        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('Dermatology', 'MAX_TOKENS'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        client.post('/symptoms', data={'symptoms': 'Rash', 'severity': 'mild', 'description': ''}, follow_redirects=True)

        with app_module.app.app_context():
            entry = app_module.SymptomLog.query.order_by(app_module.SymptomLog.created_at.desc()).first()
            # Falls through to the deterministic mapping, which also says
            # Dermatology for Rash - same answer, but via the safety net.
            assert entry.suggested_specialty == 'Dermatology'

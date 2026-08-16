"""
Tests for the on-demand AI trend explanation feature (My Health "Explain
My Recent Trends" button). Reuses the same fake Gemini client pattern
established in test_symptom_checker.py / test_specialty_matching.py.
"""
from datetime import datetime
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

    def generate_content(self, **kwargs):
        self.call_count += 1
        return self._response


class _FakeGeminiClient:
    def __init__(self, response=None):
        self.models = _FakeModels(response=response)


class TestExplainTrendsRoute:
    def test_successful_explanation_is_flashed(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            vital = app_module.Vital(patient_id=patient_id, systolic=118, diastolic=76, heart_rate=72, recorded_at=datetime.utcnow())
            app_module.db.session.add(vital)
            app_module.db.session.commit()

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse(
            'Your blood pressure has been stable and within a healthy range recently.', 'STOP'
        ))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        login(client, 'patient@example.com', 'password123')
        resp = client.post('/my-health/explain-trends', follow_redirects=True)
        assert resp.status_code == 200
        assert b'stable and within a healthy range' in resp.data

    def test_no_data_shows_graceful_fallback_not_error(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.post('/my-health/explain-trends', follow_redirects=True)
        assert resp.status_code == 200
        assert b'Not enough recent vitals or symptom history' in resp.data

    def test_ai_unavailable_shows_graceful_fallback(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            vital = app_module.Vital(patient_id=patient_id, systolic=118, diastolic=76, recorded_at=datetime.utcnow())
            app_module.db.session.add(vital)
            app_module.db.session.commit()

        monkeypatch.setattr(app_module, 'gemini_client', None)

        login(client, 'patient@example.com', 'password123')
        resp = client.post('/my-health/explain-trends', follow_redirects=True)
        assert resp.status_code == 200
        assert b'Not enough recent vitals or symptom history' in resp.data

    def test_requires_login(self, client):
        resp = client.post('/my-health/explain-trends', follow_redirects=False)
        assert resp.status_code == 302

    def test_doctor_cannot_access_patient_only_route(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doc@example.com', 'password123')
        resp = client.post('/my-health/explain-trends', follow_redirects=False)
        assert resp.status_code == 302


class TestGetAiTrendExplanationFunction:
    def test_returns_none_with_no_data_at_all(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            result = app_module.get_ai_trend_explanation(patient_id)
            assert result is None

    def test_returns_none_when_gemini_unavailable(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            vital = app_module.Vital(patient_id=patient_id, systolic=118, diastolic=76, recorded_at=datetime.utcnow())
            app_module.db.session.add(vital)
            app_module.db.session.commit()

        monkeypatch.setattr(app_module, 'gemini_client', None)

        with app_module.app.app_context():
            result = app_module.get_ai_trend_explanation(patient_id)
            assert result is None

    def test_only_uses_the_requesting_patients_own_data(self, client, make_user, monkeypatch):
        patient_a_id = make_user('patienta@example.com', 'password123', role='patient')
        patient_b_id = make_user('patientb@example.com', 'password123', role='patient')

        with app_module.app.app_context():
            vital_a = app_module.Vital(patient_id=patient_a_id, systolic=200, diastolic=120, recorded_at=datetime.utcnow())
            app_module.db.session.add(vital_a)
            app_module.db.session.commit()

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('Explanation text here.', 'STOP'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        with app_module.app.app_context():
            # Patient B has no vitals of their own - should return None (no
            # data), not accidentally use patient A's readings, and should
            # never even call the AI since there's nothing to explain.
            result = app_module.get_ai_trend_explanation(patient_b_id)
            assert result is None
            assert fake_client.models.call_count == 0

    def test_ai_response_with_bad_finish_reason_returns_none(self, client, make_user, monkeypatch):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            vital = app_module.Vital(patient_id=patient_id, systolic=118, diastolic=76, recorded_at=datetime.utcnow())
            app_module.db.session.add(vital)
            app_module.db.session.commit()

        fake_client = _FakeGeminiClient(response=_FakeGeminiResponse('Truncated output', 'MAX_TOKENS'))
        monkeypatch.setattr(app_module, 'gemini_client', fake_client)

        with app_module.app.app_context():
            result = app_module.get_ai_trend_explanation(patient_id)
            assert result is None

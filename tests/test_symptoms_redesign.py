"""
Tests for the Symptom Checker redesign: new palette applied, and the
AI-assisted badge switched from an off-palette purple to the
established informational treatment (primary text on success-light
background) used elsewhere for AI-related badges.
"""
from datetime import datetime
import app as app_module
from conftest import login


class TestSymptomsUsesThemeVariables:
    def test_no_hardcoded_light_mode_colors(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/symptoms')
        text = resp.data.decode()
        assert '#334155' not in text
        assert '#2563eb' not in text
        assert '#e2e8f0' not in text
        assert 'var(--primary)' in text

    def test_no_off_palette_purple_ai_badge(self, client, make_user):
        # The old AI-assisted badge used purple (#ede9fe/#6d28d9), which
        # is outside the agreed indigo/marigold/sage/terracotta palette -
        # switched to the same informational treatment used for AI
        # badges elsewhere in the app.
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            log = app_module.SymptomLog(
                patient_id=patient_id, symptoms='Fever', severity='mild',
                guidance='Rest', ai_generated=True, created_at=datetime.utcnow()
            )
            app_module.db.session.add(log)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/symptoms')
        text = resp.data.decode()
        assert 'AI-assisted' in text
        assert '#ede9fe' not in text
        assert '#6d28d9' not in text


class TestSymptomsFormStillWorks:
    def test_symptom_checkboxes_present(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/symptoms')
        assert b'name="symptoms"' in resp.data
        assert b'name="severity"' in resp.data

    def test_specialty_suggestion_link_present(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            log = app_module.SymptomLog(
                patient_id=patient_id, symptoms='Rash', severity='mild',
                guidance='See a dermatologist', suggested_specialty='Dermatology',
                created_at=datetime.utcnow()
            )
            app_module.db.session.add(log)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/symptoms')
        assert b'Dermatology' in resp.data
        assert b'Consider seeing a' in resp.data


class TestSymptomsEmptyState:
    def test_no_history_shows_empty_message(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/symptoms')
        assert b'No symptoms logged yet' in resp.data

"""
Tests for the Profile redesign: new palette applied, fixing the Simple
Mode toggle button (previously hardcoded #2563eb regardless of theme)
and the Delete Account section (previously hardcoded #fecaca/#b91c1c),
plus widening the layout from its old fixed 480px column to match the
width other redesigned pages use within the sidebar layout.
"""
import app as app_module
from conftest import login


class TestProfileUsesThemeVariables:
    def test_simple_mode_button_not_hardcoded(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/profile')
        text = resp.data.decode()
        assert '#2563eb' not in text
        assert 'var(--primary)' in text

    def test_delete_account_section_not_hardcoded(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/profile')
        text = resp.data.decode()
        assert '#fecaca' not in text
        assert '#b91c1c' not in text
        assert 'var(--danger)' in text


class TestProfileRoleGating:
    def test_doctor_does_not_see_simple_mode_or_deletion(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doc@example.com', 'password123')
        resp = client.get('/profile')
        assert b'Simple Mode' not in resp.data
        assert b'Delete My Account' not in resp.data

    def test_patient_sees_simple_mode_and_deletion(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/profile')
        assert b'Simple Mode' in resp.data
        assert b'Delete My Account' in resp.data


class TestProfileFormsPreserved:
    def test_password_change_fields_present(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/profile')
        assert b'name="current_password"' in resp.data
        assert b'name="new_password"' in resp.data
        assert b'name="confirm_password"' in resp.data

    def test_simple_mode_toggle_reflects_current_state(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            user = app_module.db.session.get(app_module.User, patient_id)
            user.simple_mode = True
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/profile')
        assert b'Simple Mode is ON' in resp.data

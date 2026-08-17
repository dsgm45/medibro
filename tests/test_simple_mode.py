"""
Tests for Simple Mode: an optional, per-patient accessibility toggle
(larger sidebar nav and page headers, off by default) motivated by
research on WhatsApp-familiar, larger-target interaction patterns
helping adoption among older Indian users.
"""
import app as app_module
from conftest import login


class TestToggleRoute:
    def test_toggle_on_from_off(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        client.post('/profile/toggle-simple-mode', follow_redirects=True)

        with app_module.app.app_context():
            user = app_module.db.session.get(app_module.User, patient_id)
            assert user.simple_mode is True

    def test_toggle_off_from_on(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            user = app_module.db.session.get(app_module.User, patient_id)
            user.simple_mode = True
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        client.post('/profile/toggle-simple-mode', follow_redirects=True)

        with app_module.app.app_context():
            user = app_module.db.session.get(app_module.User, patient_id)
            assert user.simple_mode is False

    def test_defaults_to_off_for_new_patients(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            user = app_module.db.session.get(app_module.User, patient_id)
            assert user.simple_mode is False

    def test_doctor_cannot_access_toggle(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doc@example.com', 'password123')
        resp = client.post('/profile/toggle-simple-mode', follow_redirects=False)
        assert resp.status_code == 302

    def test_requires_login(self, client):
        resp = client.post('/profile/toggle-simple-mode', follow_redirects=False)
        assert resp.status_code == 302


class TestSimpleModeAppliedToPages:
    def test_body_class_applied_when_enabled(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            user = app_module.db.session.get(app_module.User, patient_id)
            user.simple_mode = True
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        assert b'<body class="simple-mode">' in resp.data

    def test_body_class_absent_when_disabled(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        assert b'<body class="simple-mode">' not in resp.data
        assert b'<body>' in resp.data

    def test_does_not_leak_to_other_patients(self, client, make_user):
        patient_a_id = make_user('patienta@example.com', 'password123', role='patient')
        make_user('patientb@example.com', 'password123', role='patient')

        with app_module.app.app_context():
            user_a = app_module.db.session.get(app_module.User, patient_a_id)
            user_a.simple_mode = True
            app_module.db.session.commit()

        login(client, 'patientb@example.com', 'password123')
        resp = client.get('/my-health')
        assert b'<body class="simple-mode">' not in resp.data

    def test_doctors_never_see_simple_mode_regardless_of_field(self, client, make_user):
        # Simple Mode is patient-only by design - the context processor
        # should never apply it for a doctor session, even if the field
        # were somehow set true on that user's record.
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        with app_module.app.app_context():
            user = app_module.db.session.get(app_module.User, doctor_id)
            user.simple_mode = True
            app_module.db.session.commit()

        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor')
        assert b'<body class="simple-mode">' not in resp.data

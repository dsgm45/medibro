"""Tests for role-based access control - the core security boundary of the app."""
from conftest import login


class TestUnauthenticatedAccess:
    def test_my_health_requires_login(self, client):
        resp = client.get('/my-health', follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.location

    def test_doctor_dashboard_requires_login(self, client):
        resp = client.get('/doctor', follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.location

    def test_admin_dashboard_requires_login(self, client):
        resp = client.get('/admin', follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.location


class TestCrossRoleAccess:
    def test_patient_cannot_access_doctor_dashboard(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.get('/doctor', follow_redirects=False)
        assert resp.status_code == 302
        assert '/doctor' not in resp.location or resp.location == '/'

    def test_patient_cannot_access_admin_dashboard(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.get('/admin', follow_redirects=False)
        assert resp.status_code == 302

    def test_doctor_cannot_access_admin_dashboard(self, client, make_user):
        make_user('doctor@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doctor@example.com', 'password123')

        resp = client.get('/admin', follow_redirects=False)
        assert resp.status_code == 302

    def test_doctor_cannot_access_patient_dashboard(self, client, make_user):
        make_user('doctor@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doctor@example.com', 'password123')

        resp = client.get('/patient', follow_redirects=False)
        assert resp.status_code == 302

    def test_admin_cannot_access_patient_dashboard(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        resp = client.get('/patient', follow_redirects=False)
        assert resp.status_code == 302


class TestSameRoleAccess:
    def test_patient_can_access_own_dashboard(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.get('/my-health')
        assert resp.status_code == 200

    def test_doctor_can_access_own_dashboard(self, client, make_user):
        make_user('doctor@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doctor@example.com', 'password123')

        resp = client.get('/doctor')
        assert resp.status_code == 200

    def test_admin_can_access_own_dashboard(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        resp = client.get('/admin')
        assert resp.status_code == 200


class TestOwnershipChecks:
    """A logged-in user of the CORRECT role trying to access another
    user's specific data - a different, equally important boundary from
    role checks."""

    def test_patient_cannot_cancel_another_patients_appointment(self, client, make_user):
        import app as app_module

        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        patient_a_id = make_user('patienta@example.com', 'password123', role='patient')
        make_user('patientb@example.com', 'password123', role='patient')

        with app_module.app.app_context():
            appt = app_module.Appointment(
                patient_id=patient_a_id, doctor_id=doctor_id,
                appointment_date='2026-12-01', appointment_time='10:00',
                phone_number='555-0000', status='pending'
            )
            app_module.db.session.add(appt)
            app_module.db.session.commit()
            appt_id = appt.id

        # Log in as patient B and try to cancel patient A's appointment
        login(client, 'patientb@example.com', 'password123')
        client.post(f'/my-appointment/{appt_id}/cancel', follow_redirects=True)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            assert appt.status == 'pending'  # unchanged - patient B was not authorized

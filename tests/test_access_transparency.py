"""
Tests for patient-visible access transparency: a doctor viewing a
patient's history, or an admin viewing a patient's notes, creates an
entry the patient can see themselves. Covers both trigger points, the
patient's own view of the log, isolation between patients, and login
requirements.
"""
import app as app_module
from conftest import login


def _make_completed_appointment(patient_id, doctor_id):
    with app_module.app.app_context():
        appt = app_module.Appointment(
            patient_id=patient_id, doctor_id=doctor_id,
            appointment_date='2026-08-01', appointment_time='10:00',
            phone_number='555', status='completed'
        )
        app_module.db.session.add(appt)
        app_module.db.session.commit()
        return appt.id


class TestDoctorViewLogged:
    def test_doctor_viewing_patient_history_creates_log_entry(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_completed_appointment(patient_id, doctor_id)

        login(client, 'doc@example.com', 'password123')
        client.get(f'/doctor/patient/{patient_id}')

        with app_module.app.app_context():
            entry = app_module.PatientDataAccessLog.query.filter_by(patient_id=patient_id).first()
            assert entry is not None
            assert entry.viewer_id == doctor_id
            assert entry.viewer_role == 'doctor'

    def test_unauthorized_doctor_view_does_not_create_log_entry(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        make_user('otherdoc@example.com', 'password123', role='doctor', status='approved')

        login(client, 'otherdoc@example.com', 'password123')
        client.get(f'/doctor/patient/{patient_id}')

        with app_module.app.app_context():
            assert app_module.PatientDataAccessLog.query.filter_by(patient_id=patient_id).count() == 0

    def test_repeated_views_each_create_their_own_entry(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_completed_appointment(patient_id, doctor_id)

        login(client, 'doc@example.com', 'password123')
        client.get(f'/doctor/patient/{patient_id}')
        client.get(f'/doctor/patient/{patient_id}')

        with app_module.app.app_context():
            assert app_module.PatientDataAccessLog.query.filter_by(patient_id=patient_id).count() == 2


class TestAdminViewLogged:
    def test_admin_viewing_patient_notes_creates_log_entry(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        admin_id = make_user('admin@example.com', 'password123', role='hospital', status='approved')

        login(client, 'admin@example.com', 'password123')
        client.get(f'/admin/patient/{patient_id}/notes')

        with app_module.app.app_context():
            entry = app_module.PatientDataAccessLog.query.filter_by(patient_id=patient_id).first()
            assert entry is not None
            assert entry.viewer_id == admin_id
            assert entry.viewer_role == 'admin'


class TestPatientOwnAccessLogView:
    def test_patient_sees_their_own_access_log(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved', full_name='Dr. Sharma')
        _make_completed_appointment(patient_id, doctor_id)

        login(client, 'doc@example.com', 'password123')
        client.get(f'/doctor/patient/{patient_id}')
        client.post('/logout', follow_redirects=True)

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health/access-log')
        assert resp.status_code == 200
        assert b'Sharma' in resp.data

    def test_patient_only_sees_their_own_log_not_another_patients(self, client, make_user):
        patient_a_id = make_user('patienta@example.com', 'password123', role='patient')
        patient_b_id = make_user('patientb@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_completed_appointment(patient_a_id, doctor_id)

        login(client, 'doc@example.com', 'password123')
        client.get(f'/doctor/patient/{patient_a_id}')
        client.post('/logout', follow_redirects=True)

        login(client, 'patientb@example.com', 'password123')
        resp = client.get('/my-health/access-log')
        assert resp.status_code == 200
        assert b'No one has viewed your records yet' in resp.data

    def test_access_log_requires_login(self, client):
        resp = client.get('/my-health/access-log', follow_redirects=False)
        assert resp.status_code == 302

    def test_doctor_cannot_access_patient_only_route(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doc@example.com', 'password123')
        resp = client.get('/my-health/access-log', follow_redirects=False)
        assert resp.status_code == 302

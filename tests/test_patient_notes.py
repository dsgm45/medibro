"""
Tests for the patient notes feature: a private free-text note a patient
can leave on each completed appointment, visible to them, the specific
treating doctor (not other doctors, even for the same patient's other
visits), and admin (for oversight, reachable from the doctor/patient
rows on the admin dashboard).
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


class TestSavingNotes:
    def test_patient_can_save_a_note(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_completed_appointment(patient_id, doctor_id)

        login(client, 'patient@example.com', 'password123')
        client.post(f'/my-appointment/{appt_id}/note', data={'patient_note': 'Felt much better after this visit'}, follow_redirects=True)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            assert appt.patient_note == 'Felt much better after this visit'

    def test_patient_can_edit_an_existing_note(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_completed_appointment(patient_id, doctor_id)

        login(client, 'patient@example.com', 'password123')
        client.post(f'/my-appointment/{appt_id}/note', data={'patient_note': 'First version'}, follow_redirects=True)
        client.post(f'/my-appointment/{appt_id}/note', data={'patient_note': 'Updated version'}, follow_redirects=True)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            assert appt.patient_note == 'Updated version'

    def test_cannot_save_note_on_another_patients_appointment(self, client, make_user):
        owner_id = make_user('owner@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('attacker@example.com', 'password123', role='patient')
        appt_id = _make_completed_appointment(owner_id, doctor_id)

        login(client, 'attacker@example.com', 'password123')
        client.post(f'/my-appointment/{appt_id}/note', data={'patient_note': 'Hijacked note'}, follow_redirects=True)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            assert appt.patient_note is None

    def test_cannot_save_note_on_incomplete_appointment(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        with app_module.app.app_context():
            appt = app_module.Appointment(
                patient_id=patient_id, doctor_id=doctor_id,
                appointment_date='2026-08-01', appointment_time='10:00',
                phone_number='555', status='accepted'
            )
            app_module.db.session.add(appt)
            app_module.db.session.commit()
            appt_id = appt.id

        login(client, 'patient@example.com', 'password123')
        client.post(f'/my-appointment/{appt_id}/note', data={'patient_note': 'Too early'}, follow_redirects=True)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            assert appt.patient_note is None


class TestDoctorVisibility:
    def test_treating_doctor_sees_the_note(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_completed_appointment(patient_id, doctor_id)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            appt.patient_note = 'This doctor was great'
            app_module.db.session.commit()

        login(client, 'doc@example.com', 'password123')
        resp = client.get(f'/doctor/patient/{patient_id}')
        assert b'This doctor was great' in resp.data

    def test_other_doctor_does_not_see_the_note(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('otherdoc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_completed_appointment(patient_id, doctor_id)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            appt.patient_note = 'Private note about the first doctor'
            app_module.db.session.commit()
            # Give the other doctor a reason to be able to view this patient's history too
            other_appt = app_module.Appointment(
                patient_id=patient_id, doctor_id=app_module.User.query.filter_by(email='otherdoc@example.com').first().id,
                appointment_date='2026-08-02', appointment_time='11:00',
                phone_number='555', status='completed'
            )
            app_module.db.session.add(other_appt)
            app_module.db.session.commit()

        login(client, 'otherdoc@example.com', 'password123')
        resp = client.get(f'/doctor/patient/{patient_id}')
        assert b'Private note about the first doctor' not in resp.data


class TestAdminOversight:
    def test_admin_can_view_doctor_notes(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        appt_id = _make_completed_appointment(patient_id, doctor_id)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            appt.patient_note = 'Admin should see this'
            app_module.db.session.commit()

        login(client, 'admin@example.com', 'password123')
        resp = client.get(f'/admin/doctor/{doctor_id}/notes')
        assert resp.status_code == 200
        assert b'Admin should see this' in resp.data

    def test_admin_can_view_patient_notes(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        appt_id = _make_completed_appointment(patient_id, doctor_id)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            appt.patient_note = 'Note from this patient'
            app_module.db.session.commit()

        login(client, 'admin@example.com', 'password123')
        resp = client.get(f'/admin/patient/{patient_id}/notes')
        assert resp.status_code == 200
        assert b'Note from this patient' in resp.data

    def test_appointments_without_notes_excluded_from_admin_view(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        _make_completed_appointment(patient_id, doctor_id)  # no note ever set

        login(client, 'admin@example.com', 'password123')
        resp = client.get(f'/admin/doctor/{doctor_id}/notes')
        assert b'No notes on record' in resp.data

    def test_patient_cannot_access_admin_doctor_notes(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient@example.com', 'password123', role='patient')

        login(client, 'patient@example.com', 'password123')
        resp = client.get(f'/admin/doctor/{doctor_id}/notes', follow_redirects=False)
        assert resp.status_code == 302

    def test_patient_cannot_access_admin_patient_notes(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        make_user('other@example.com', 'password123', role='patient')

        login(client, 'other@example.com', 'password123')
        resp = client.get(f'/admin/patient/{patient_id}/notes', follow_redirects=False)
        assert resp.status_code == 302

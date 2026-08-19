"""
Tests for the Appointments page redesign: new palette applied
throughout, status badges now match the meaning-coded scheme
established on the doctor dashboard (accepted/completed=sage,
declined=terracotta, pending=marigold, cancelled=neutral), replacing
the old off-palette purple "Completed" badge.
"""
from datetime import date
import app as app_module
from conftest import login


def _make_appointment(patient_id, doctor_id, status='pending', appointment_time=None, reason='Checkup'):
    with app_module.app.app_context():
        appt = app_module.Appointment(
            patient_id=patient_id, doctor_id=doctor_id,
            appointment_date=date.today().isoformat(), appointment_time=appointment_time,
            phone_number='555-1234', reason=reason, status=status
        )
        app_module.db.session.add(appt)
        app_module.db.session.commit()
        return appt.id


class TestAppointmentsUsesThemeVariables:
    def test_no_hardcoded_light_mode_colors(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/patient')
        text = resp.data.decode()
        assert '#0f172a' not in text
        assert '#2563eb' not in text
        assert '#cbd5e1' not in text
        assert 'var(--primary)' in text

    def test_no_off_palette_purple_completed_badge(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id, status='completed')

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/patient')
        text = resp.data.decode()
        assert 'Completed' in text
        assert '#ede9fe' not in text
        assert '#6d28d9' not in text


class TestAppointmentsStatusActionsPreserved:
    def test_pending_shows_cancel(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id, status='pending')

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/patient')
        assert b'Cancel' in resp.data

    def test_accepted_shows_chat(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id, status='accepted', appointment_time='10:00')

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/patient')
        assert b'Chat' in resp.data

    def test_completed_shows_summary_and_chat(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id, status='completed', appointment_time='09:00')

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/patient')
        assert b'Summary' in resp.data
        assert b'Chat' in resp.data


class TestAppointmentsBookingFormPreserved:
    def test_booking_form_fields_present(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/patient')
        assert b'name="doctor_id"' in resp.data
        assert b'name="appointment_date"' in resp.data
        assert b'name="phone_number"' in resp.data


class TestAppointmentsEmptyState:
    def test_no_doctors_or_appointments_shows_empty_messages(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/patient')
        assert b'No verified doctors are currently available' in resp.data
        assert b'You have not booked any appointments yet' in resp.data

"""
Tests for the Doctor Dashboard redesign: converted from a data table to
a dense, urgency-coded list per the agreed mockup (quieter, denser
register than patient-side, left-edge color encodes urgency rather than
content type). Covers that every original action survived the rewrite,
since this page has unusually many interactive branches.
"""
from datetime import datetime, date
import app as app_module
from conftest import login


def _make_appointment(patient_id, doctor_id, status='pending', appointment_time=None, reason='Checkup', appointment_date=None):
    with app_module.app.app_context():
        appt = app_module.Appointment(
            patient_id=patient_id, doctor_id=doctor_id,
            appointment_date=appointment_date or date.today().isoformat(),
            appointment_time=appointment_time, phone_number='555-1234',
            reason=reason, status=status
        )
        app_module.db.session.add(appt)
        app_module.db.session.commit()
        return appt.id


class TestDoctorDashboardPreservesActions:
    def test_pending_without_time_shows_accept_and_set_time(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id, status='pending')

        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor')
        assert resp.status_code == 200
        assert b'name="assigned_time"' in resp.data
        assert b'Accept &amp; Set Time' in resp.data

    def test_accepted_shows_chat_and_complete(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id, status='accepted', appointment_time='10:00')

        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor')
        assert b'Mark Completed' in resp.data
        assert b'Chat' in resp.data

    def test_completed_without_pending_followup_shows_request_button(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_appointment(patient_id, doctor_id, status='completed', appointment_time='09:00')

        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor')
        assert b'Request Follow-up' in resp.data
        assert f'toggleFollowUpForm({appt_id})'.encode() in resp.data

    def test_refill_requests_show_approve_and_deny(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Metformin', dosage='500mg', frequency='daily', start_date=date.today())
            app_module.db.session.add(med)
            app_module.db.session.commit()
            refill = app_module.RefillRequest(medicine_id=med.id, patient_id=patient_id, doctor_id=doctor_id, status='pending', requested_at=datetime.utcnow())
            app_module.db.session.add(refill)
            app_module.db.session.commit()

        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor')
        assert b'Approve' in resp.data
        assert b'Deny' in resp.data
        assert b'Metformin' in resp.data


class TestDoctorDashboardUsesThemeVariables:
    def test_no_hardcoded_light_mode_colors(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id, status='pending')

        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor')
        text = resp.data.decode()
        assert '#2563eb' not in text
        assert '#0f172a' not in text
        assert 'var(--primary)' in text

    def test_no_emoji_present(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor')
        text = resp.data.decode()
        for emoji in ['⚙', '🩺', '💊', '📝', '💬']:
            assert emoji not in text


class TestDoctorDashboardEmptyState:
    def test_no_appointments_shows_empty_message(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor')
        assert b'No appointment requests received yet' in resp.data

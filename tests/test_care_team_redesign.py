"""
Tests for the Care Team consistency pass: this page still had the old
palette (blue avatars, old card colors) since it was built before this
redesign existed. Updated to the marigold avatar ring treatment
established in My Health's Next Appointment card, for consistency.
"""
import app as app_module
from conftest import login


def _make_appointment(patient_id, doctor_id, status='completed', date='2026-08-01'):
    with app_module.app.app_context():
        appt = app_module.Appointment(
            patient_id=patient_id, doctor_id=doctor_id,
            appointment_date=date, phone_number='555', status=status
        )
        app_module.db.session.add(appt)
        app_module.db.session.commit()
        return appt.id


class TestCareTeamUsesThemeVariables:
    def test_no_hardcoded_light_mode_colors(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id)

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health/care-team')
        text = resp.data.decode()
        assert '#2563eb' not in text
        assert '#0f172a' not in text
        assert '#cbd5e1' not in text
        assert 'var(--primary)' in text

    def test_avatar_uses_marigold_accent_matching_my_health(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id)

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health/care-team')
        text = resp.data.decode()
        assert 'background-color: var(--accent)' in text


class TestCareTeamStillFunctionsCorrectly:
    def test_shows_doctor_name_and_specialty(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved', full_name='Dr. Anjali Sharma')
        _make_appointment(patient_id, doctor_id)

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health/care-team')
        assert b'Anjali Sharma' in resp.data

    def test_book_again_link_present(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id)

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health/care-team')
        assert b'Book Again' in resp.data

    def test_empty_state_message(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health/care-team')
        assert b"haven't had any appointments yet" in resp.data

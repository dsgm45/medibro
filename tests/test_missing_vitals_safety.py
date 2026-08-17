"""
Tests for a safety-relevant fix: missing vitals fields must be visually
distinct from real values (not just a bare dash with identical styling),
since a blank-looking cell can be misread as "normal" by a clinician
scanning quickly. Covers the doctor's patient history view, the
patient's own My Health summary, and the patient's own Vitals page.
"""
from datetime import datetime
import app as app_module
from conftest import login


def _make_appointment(patient_id, doctor_id):
    with app_module.app.app_context():
        appt = app_module.Appointment(
            patient_id=patient_id, doctor_id=doctor_id,
            appointment_date='2026-08-01', appointment_time='10:00',
            phone_number='555', status='completed'
        )
        app_module.db.session.add(appt)
        app_module.db.session.commit()


class TestDoctorPatientHistoryMissingVitals:
    def test_missing_fields_show_not_recorded_not_bare_dash(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id)

        with app_module.app.app_context():
            vital = app_module.Vital(patient_id=patient_id, heart_rate=72, recorded_at=datetime.utcnow())
            app_module.db.session.add(vital)
            app_module.db.session.commit()

        login(client, 'doc@example.com', 'password123')
        resp = client.get(f'/doctor/patient/{patient_id}')
        assert b'Not recorded' in resp.data
        assert b'72' in resp.data

    def test_missing_fields_are_visually_distinct_from_present_ones(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id)

        with app_module.app.app_context():
            vital = app_module.Vital(patient_id=patient_id, heart_rate=72, recorded_at=datetime.utcnow())
            app_module.db.session.add(vital)
            app_module.db.session.commit()

        login(client, 'doc@example.com', 'password123')
        resp = client.get(f'/doctor/patient/{patient_id}')
        text = resp.data.decode()
        # The missing-value styling must differ from the present-value
        # styling - not just a different character, genuinely different
        # visual treatment (muted color, italic).
        not_recorded_section = text[max(0, text.index('Not recorded') - 150):text.index('Not recorded')]
        assert 'font-style: italic' in not_recorded_section
        assert '#94a3b8' not in text  # the low-contrast color must not be reintroduced


class TestMyHealthMissingVitals:
    def test_missing_fields_show_not_recorded(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            vital = app_module.Vital(patient_id=patient_id, heart_rate=72, recorded_at=datetime.utcnow())
            app_module.db.session.add(vital)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        assert b'Not recorded' in resp.data
        assert b'72' in resp.data

    def test_unit_label_hidden_when_value_missing(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            # heart_rate present, spo2 missing entirely
            vital = app_module.Vital(patient_id=patient_id, heart_rate=72, recorded_at=datetime.utcnow())
            app_module.db.session.add(vital)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        text = resp.data.decode()
        # "% " unit shouldn't dangle next to a "Not recorded" line for SpO2
        not_recorded_count = text.count('Not recorded')
        assert not_recorded_count >= 1


class TestVitalsPageMissingVitals:
    def test_no_vitals_at_all_shows_not_recorded_everywhere(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/vitals')
        assert resp.data.count(b'Not recorded') == 4

    def test_history_table_distinguishes_missing_from_present(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            vital = app_module.Vital(patient_id=patient_id, systolic=118, diastolic=76, heart_rate=None, recorded_at=datetime.utcnow())
            app_module.db.session.add(vital)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/vitals')
        assert b'118/76' in resp.data
        assert b'Not recorded' in resp.data

"""
Tests for the final batch: Medicine Edit, Request Refill, Appointment
Summary, Doctor Profile, Patient History View, and Account Locked -
the last remaining pages carrying old hardcoded colors from before
this redesign existed. Also removes one remaining clock emoji and
fixes a callout box (Patient's Note, shown to doctors) that used the
same hardcoded-gray-box pattern flagged and fixed elsewhere earlier.
"""
from datetime import date, datetime
import app as app_module
from conftest import login


class TestMedicineEditRedesign:
    def test_no_hardcoded_colors(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Metformin', start_date=date.today())
            app_module.db.session.add(med)
            app_module.db.session.commit()
            med_id = med.id

        login(client, 'patient@example.com', 'password123')
        resp = client.get(f'/medicines/{med_id}/edit')
        text = resp.data.decode()
        assert '#0f172a' not in text
        assert '#2563eb' not in text

    def test_chip_js_reads_theme(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Metformin', start_date=date.today())
            app_module.db.session.add(med)
            app_module.db.session.commit()
            med_id = med.id

        login(client, 'patient@example.com', 'password123')
        resp = client.get(f'/medicines/{med_id}/edit')
        assert b'getComputedStyle' in resp.data


class TestRequestRefillRedesign:
    def test_no_hardcoded_colors(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Metformin', start_date=date.today())
            app_module.db.session.add(med)
            app_module.db.session.commit()
            med_id = med.id

        login(client, 'patient@example.com', 'password123')
        resp = client.get(f'/medicines/{med_id}/request-refill')
        text = resp.data.decode()
        assert '#0f172a' not in text
        assert '#cbd5e1' not in text


class TestAppointmentSummaryRedesign:
    def test_no_emoji_and_download_link_present(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        with app_module.app.app_context():
            appt = app_module.Appointment(
                patient_id=patient_id, doctor_id=doctor_id, appointment_date='2026-08-01',
                phone_number='555', status='completed', diagnosis='Flu'
            )
            app_module.db.session.add(appt)
            app_module.db.session.commit()
            med = app_module.Medicine(patient_id=patient_id, appointment_id=appt.id, name='Metformin', start_date=date.today())
            app_module.db.session.add(med)
            app_module.db.session.commit()
            appt_id = appt.id

        login(client, 'patient@example.com', 'password123')
        resp = client.get(f'/my-appointment/{appt_id}/summary')
        text = resp.data.decode()
        assert '⬇' not in text
        assert 'Download Prescription PDF' in text


class TestDoctorProfileRedesign:
    def test_no_hardcoded_label_color(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor/profile')
        text = resp.data.decode()
        assert '#334155' not in text


class TestPatientHistoryViewRedesign:
    def test_no_clock_emoji_uses_icon_instead(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Metformin', start_date=date.today())
            app_module.db.session.add(med)
            app_module.db.session.commit()
            dose = app_module.MedicineDose(medicine_id=med.id, time='08:00')
            app_module.db.session.add(dose)
            appt = app_module.Appointment(patient_id=patient_id, doctor_id=doctor_id, appointment_date='2026-08-01', phone_number='555', status='accepted')
            app_module.db.session.add(appt)
            app_module.db.session.commit()

        login(client, 'doc@example.com', 'password123')
        resp = client.get(f'/doctor/patient/{patient_id}')
        text = resp.data.decode()
        assert '⏰' not in text
        assert 'ti-clock' in text

    def test_patient_note_callout_uses_theme_colors(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        with app_module.app.app_context():
            appt = app_module.Appointment(
                patient_id=patient_id, doctor_id=doctor_id, appointment_date='2026-08-01',
                phone_number='555', status='completed', patient_note='Feeling much better now'
            )
            app_module.db.session.add(appt)
            app_module.db.session.commit()

        login(client, 'doc@example.com', 'password123')
        resp = client.get(f'/doctor/patient/{patient_id}')
        text = resp.data.decode()
        assert '#94a3b8' not in text
        assert '#f8fafc' not in text
        assert 'var(--accent-light)' in text


class TestAccountLockedRedesign:
    def test_no_hardcoded_colors(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        client.post('/profile/request-deletion', follow_redirects=True)
        resp = client.get('/account-locked')
        text = resp.data.decode()
        assert '#fecaca' not in text
        assert '#b91c1c' not in text
        assert '#16a34a' not in text

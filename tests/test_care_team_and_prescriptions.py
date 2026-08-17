"""
Tests for Batch 5: Care Team view, document search, and structured
per-visit prescriptions (medicines now linked to the specific
appointment they were prescribed at, via Medicine.appointment_id).
"""
import app as app_module
from conftest import login


def _make_appointment(patient_id, doctor_id, status='completed', date='2026-08-01'):
    with app_module.app.app_context():
        appt = app_module.Appointment(
            patient_id=patient_id, doctor_id=doctor_id,
            appointment_date=date, appointment_time='10:00',
            phone_number='555', status=status
        )
        app_module.db.session.add(appt)
        app_module.db.session.commit()
        return appt.id


class TestCareTeam:
    def test_shows_one_entry_per_distinct_doctor(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved', full_name='Dr. Sharma')
        _make_appointment(patient_id, doctor_id, status='completed', date='2026-07-01')
        _make_appointment(patient_id, doctor_id, status='completed', date='2026-08-01')

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health/care-team')
        assert resp.data.count(b'Sharma') == 1  # one entry, not two, despite two appointments
        assert b'2026-08-01' in resp.data  # the most recent visit date

    def test_chat_link_only_shown_for_chat_eligible_appointment(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_appointment(patient_id, doctor_id, status='pending')

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health/care-team')
        assert b'>Chat<' not in resp.data

    def test_empty_state_with_no_appointments(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health/care-team')
        assert b"haven't had any appointments" in resp.data

    def test_requires_login(self, client):
        resp = client.get('/my-health/care-team', follow_redirects=False)
        assert resp.status_code == 302


class TestDocumentSearch:
    def test_search_filters_by_filename(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            doc1 = app_module.Document(patient_id=patient_id, original_filename='xray_chest.pdf', file_data=b'x', file_size=1)
            doc2 = app_module.Document(patient_id=patient_id, original_filename='blood_test.pdf', file_data=b'x', file_size=1)
            app_module.db.session.add_all([doc1, doc2])
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health?doc_search=xray')
        assert b'xray_chest.pdf' in resp.data
        assert b'blood_test.pdf' not in resp.data

    def test_search_is_case_insensitive(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            doc = app_module.Document(patient_id=patient_id, original_filename='XRay_Chest.pdf', file_data=b'x', file_size=1)
            app_module.db.session.add(doc)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health?doc_search=xray')
        assert b'XRay_Chest.pdf' in resp.data

    def test_no_search_shows_all_documents(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            doc1 = app_module.Document(patient_id=patient_id, original_filename='a.pdf', file_data=b'x', file_size=1)
            doc2 = app_module.Document(patient_id=patient_id, original_filename='b.pdf', file_data=b'x', file_size=1)
            app_module.db.session.add_all([doc1, doc2])
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        assert b'a.pdf' in resp.data and b'b.pdf' in resp.data


class TestStructuredPrescription:
    def test_completing_visit_creates_linked_medicines(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_appointment(patient_id, doctor_id, status='accepted')

        login(client, 'doc@example.com', 'password123')
        client.post(f'/appointment/{appt_id}/complete', data={
            'diagnosis': 'Diabetes', 'visit_notes': 'Follow up in 2 weeks',
            'med_name_1': 'Metformin', 'med_dosage_1': '500mg', 'med_frequency_1': 'Twice daily',
            'med_name_2': '', 'med_name_3': '', 'med_name_4': ''
        }, follow_redirects=True)

        with app_module.app.app_context():
            meds = app_module.Medicine.query.filter_by(appointment_id=appt_id).all()
            assert len(meds) == 1
            assert meds[0].name == 'Metformin'
            assert meds[0].patient_id == patient_id

    def test_blank_medicine_rows_are_skipped(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_appointment(patient_id, doctor_id, status='accepted')

        login(client, 'doc@example.com', 'password123')
        client.post(f'/appointment/{appt_id}/complete', data={
            'diagnosis': 'Checkup', 'visit_notes': '',
            'med_name_1': '', 'med_name_2': '', 'med_name_3': '', 'med_name_4': ''
        }, follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.Medicine.query.filter_by(appointment_id=appt_id).count() == 0

    def test_resaving_visit_replaces_medicines_not_duplicates(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_appointment(patient_id, doctor_id, status='accepted')

        login(client, 'doc@example.com', 'password123')
        client.post(f'/appointment/{appt_id}/complete', data={
            'diagnosis': 'Diabetes', 'visit_notes': '',
            'med_name_1': 'Metformin', 'med_name_2': '', 'med_name_3': '', 'med_name_4': ''
        }, follow_redirects=True)
        client.post(f'/appointment/{appt_id}/complete', data={
            'diagnosis': 'Diabetes', 'visit_notes': '',
            'med_name_1': 'Insulin', 'med_name_2': '', 'med_name_3': '', 'med_name_4': ''
        }, follow_redirects=True)

        with app_module.app.app_context():
            meds = app_module.Medicine.query.filter_by(appointment_id=appt_id).all()
            assert len(meds) == 1
            assert meds[0].name == 'Insulin'

    def test_prescribed_medicine_also_appears_in_patients_own_medicine_list(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_appointment(patient_id, doctor_id, status='accepted')

        login(client, 'doc@example.com', 'password123')
        client.post(f'/appointment/{appt_id}/complete', data={
            'diagnosis': 'Diabetes', 'visit_notes': '',
            'med_name_1': 'Metformin', 'med_name_2': '', 'med_name_3': '', 'med_name_4': ''
        }, follow_redirects=True)

        client.post('/logout', follow_redirects=True)
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/medicines')
        assert b'Metformin' in resp.data


class TestPrescriptionPdf:
    def test_patient_can_download_prescription(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_appointment(patient_id, doctor_id, status='completed')
        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            appt.diagnosis = 'Diabetes'
            med = app_module.Medicine(patient_id=patient_id, appointment_id=appt_id, name='Metformin', dosage='500mg')
            app_module.db.session.add(med)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get(f'/my-appointment/{appt_id}/prescription-pdf')
        assert resp.status_code == 200
        assert resp.headers.get('Content-Type') == 'application/pdf'

    def test_doctor_can_download_prescription_for_own_patient(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_appointment(patient_id, doctor_id, status='completed')

        login(client, 'doc@example.com', 'password123')
        resp = client.get(f'/my-appointment/{appt_id}/prescription-pdf')
        assert resp.status_code == 200

    def test_other_patient_cannot_download(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('attacker@example.com', 'password123', role='patient')
        appt_id = _make_appointment(patient_id, doctor_id, status='completed')

        login(client, 'attacker@example.com', 'password123')
        resp = client.get(f'/my-appointment/{appt_id}/prescription-pdf', follow_redirects=False)
        assert resp.status_code == 302

    def test_other_doctor_cannot_download(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('otherdoc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_appointment(patient_id, doctor_id, status='completed')

        login(client, 'otherdoc@example.com', 'password123')
        resp = client.get(f'/my-appointment/{appt_id}/prescription-pdf', follow_redirects=False)
        assert resp.status_code == 302

    def test_cannot_download_for_incomplete_visit(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_appointment(patient_id, doctor_id, status='accepted')

        login(client, 'patient@example.com', 'password123')
        resp = client.get(f'/my-appointment/{appt_id}/prescription-pdf', follow_redirects=True)
        assert b'does not have a prescription yet' in resp.data


class TestAccountDeletionWithPrescribedMedicines:
    def test_deleting_account_with_appointment_linked_medicine_does_not_crash(self, client, make_user):
        # The real risk this batch introduced: Medicine.appointment_id now
        # references Appointment, so the account-deletion cascade order
        # matters - Medicine must be deleted before Appointment, or this
        # would violate the foreign key constraint in production Postgres.
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        appt_id = _make_appointment(patient_id, doctor_id, status='completed')

        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, appointment_id=appt_id, name='Metformin', dosage='500mg')
            app_module.db.session.add(med)
            app_module.db.session.commit()

        with app_module.app.app_context():
            result = app_module._permanently_delete_patient_account(patient_id)
            assert result is True
            assert app_module.db.session.get(app_module.User, patient_id) is None
            assert app_module.Medicine.query.filter_by(patient_id=patient_id).count() == 0
            assert app_module.Appointment.query.filter_by(patient_id=patient_id).count() == 0

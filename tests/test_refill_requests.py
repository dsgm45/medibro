"""
Tests for prescription refill requests: a patient asks a specific
treating doctor (one they've actually had appointments with, not any
arbitrary doctor) to refill an existing medicine. Approving extends the
medicine's end date by 30 days from whichever is later - the current
end date or today, so approvals never lose already-elapsed time and
never double-count a gap if the medicine had already lapsed.
"""
from datetime import timedelta
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


def _make_medicine(patient_id, end_date=None):
    with app_module.app.app_context():
        med = app_module.Medicine(patient_id=patient_id, name='Metformin', dosage='500mg', end_date=end_date)
        app_module.db.session.add(med)
        app_module.db.session.commit()
        return med.id


class TestRequestRefillForm:
    def test_shows_only_doctors_with_appointment_history(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        treating_doctor_id = make_user('treatingdoc@example.com', 'password123', role='doctor', status='approved', full_name='Dr. Treating')
        make_user('unrelateddoc@example.com', 'password123', role='doctor', status='approved', full_name='Dr. Unrelated')
        _make_completed_appointment(patient_id, treating_doctor_id)
        med_id = _make_medicine(patient_id)

        login(client, 'patient@example.com', 'password123')
        resp = client.get(f'/medicines/{med_id}/request-refill')
        assert b'Treating' in resp.data
        assert b'Unrelated' not in resp.data

    def test_cannot_view_refill_form_for_another_patients_medicine(self, client, make_user):
        owner_id = make_user('owner@example.com', 'password123', role='patient')
        make_user('attacker@example.com', 'password123', role='patient')
        med_id = _make_medicine(owner_id)

        login(client, 'attacker@example.com', 'password123')
        resp = client.get(f'/medicines/{med_id}/request-refill', follow_redirects=True)
        assert b'Unauthorized' in resp.data


class TestSubmitRefillRequest:
    def test_valid_request_is_created_and_doctor_notified(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_completed_appointment(patient_id, doctor_id)
        med_id = _make_medicine(patient_id)

        login(client, 'patient@example.com', 'password123')
        client.post(f'/medicines/{med_id}/request-refill', data={'doctor_id': doctor_id, 'note': 'Running low'}, follow_redirects=True)

        with app_module.app.app_context():
            req = app_module.RefillRequest.query.filter_by(medicine_id=med_id).first()
            assert req is not None
            assert req.doctor_id == doctor_id
            assert req.status == 'pending'
            assert req.note == 'Running low'

            notif = app_module.Notification.query.filter_by(user_id=doctor_id, type='refill_request').first()
            assert notif is not None

    def test_cannot_request_from_a_doctor_with_no_appointment_history(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        make_user('treatingdoc@example.com', 'password123', role='doctor', status='approved')
        unrelated_doctor_id = make_user('unrelateddoc@example.com', 'password123', role='doctor', status='approved')
        med_id = _make_medicine(patient_id)
        # Note: no appointment made with unrelated_doctor_id

        login(client, 'patient@example.com', 'password123')
        client.post(f'/medicines/{med_id}/request-refill', data={'doctor_id': unrelated_doctor_id, 'note': ''}, follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.RefillRequest.query.filter_by(medicine_id=med_id).count() == 0

    def test_cannot_request_refill_on_another_patients_medicine(self, client, make_user):
        owner_id = make_user('owner@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('attacker@example.com', 'password123', role='patient')
        _make_completed_appointment(owner_id, doctor_id)
        med_id = _make_medicine(owner_id)

        login(client, 'attacker@example.com', 'password123')
        client.post(f'/medicines/{med_id}/request-refill', data={'doctor_id': doctor_id, 'note': ''}, follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.RefillRequest.query.filter_by(medicine_id=med_id).count() == 0


class TestRespondToRefillRequest:
    def test_approving_extends_end_date_from_today_when_already_expired(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_completed_appointment(patient_id, doctor_id)
        with app_module.app.app_context():
            expired_end = app_module.get_ist_today() - timedelta(days=5)
        med_id = _make_medicine(patient_id, end_date=expired_end)

        with app_module.app.app_context():
            req = app_module.RefillRequest(medicine_id=med_id, patient_id=patient_id, doctor_id=doctor_id)
            app_module.db.session.add(req)
            app_module.db.session.commit()
            req_id = req.id

        login(client, 'doc@example.com', 'password123')
        client.post(f'/refill-request/{req_id}/respond', data={'action': 'approve'}, follow_redirects=True)

        with app_module.app.app_context():
            med = app_module.db.session.get(app_module.Medicine, med_id)
            expected = app_module.get_ist_today() + timedelta(days=30)
            assert med.end_date == expected

    def test_approving_extends_from_existing_future_end_date_not_today(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_completed_appointment(patient_id, doctor_id)
        with app_module.app.app_context():
            future_end = app_module.get_ist_today() + timedelta(days=10)
        med_id = _make_medicine(patient_id, end_date=future_end)

        with app_module.app.app_context():
            req = app_module.RefillRequest(medicine_id=med_id, patient_id=patient_id, doctor_id=doctor_id)
            app_module.db.session.add(req)
            app_module.db.session.commit()
            req_id = req.id

        login(client, 'doc@example.com', 'password123')
        client.post(f'/refill-request/{req_id}/respond', data={'action': 'approve'}, follow_redirects=True)

        with app_module.app.app_context():
            med = app_module.db.session.get(app_module.Medicine, med_id)
            expected = future_end + timedelta(days=30)
            assert med.end_date == expected

    def test_denying_does_not_change_end_date(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_completed_appointment(patient_id, doctor_id)
        med_id = _make_medicine(patient_id, end_date=None)

        with app_module.app.app_context():
            req = app_module.RefillRequest(medicine_id=med_id, patient_id=patient_id, doctor_id=doctor_id)
            app_module.db.session.add(req)
            app_module.db.session.commit()
            req_id = req.id

        login(client, 'doc@example.com', 'password123')
        client.post(f'/refill-request/{req_id}/respond', data={'action': 'deny'}, follow_redirects=True)

        with app_module.app.app_context():
            med = app_module.db.session.get(app_module.Medicine, med_id)
            assert med.end_date is None
            req = app_module.db.session.get(app_module.RefillRequest, req_id)
            assert req.status == 'denied'

    def test_patient_notified_of_response(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_completed_appointment(patient_id, doctor_id)
        med_id = _make_medicine(patient_id)

        with app_module.app.app_context():
            req = app_module.RefillRequest(medicine_id=med_id, patient_id=patient_id, doctor_id=doctor_id)
            app_module.db.session.add(req)
            app_module.db.session.commit()
            req_id = req.id

        login(client, 'doc@example.com', 'password123')
        client.post(f'/refill-request/{req_id}/respond', data={'action': 'approve'}, follow_redirects=True)

        with app_module.app.app_context():
            notif = app_module.Notification.query.filter_by(user_id=patient_id, type='refill_response').first()
            assert notif is not None
            assert 'approved' in notif.message

    def test_unrelated_doctor_cannot_respond(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('otherdoc@example.com', 'password123', role='doctor', status='approved')
        _make_completed_appointment(patient_id, doctor_id)
        med_id = _make_medicine(patient_id)

        with app_module.app.app_context():
            req = app_module.RefillRequest(medicine_id=med_id, patient_id=patient_id, doctor_id=doctor_id)
            app_module.db.session.add(req)
            app_module.db.session.commit()
            req_id = req.id

        login(client, 'otherdoc@example.com', 'password123')
        client.post(f'/refill-request/{req_id}/respond', data={'action': 'approve'}, follow_redirects=True)

        with app_module.app.app_context():
            req = app_module.db.session.get(app_module.RefillRequest, req_id)
            assert req.status == 'pending'

    def test_cannot_respond_to_already_resolved_request(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        _make_completed_appointment(patient_id, doctor_id)
        med_id = _make_medicine(patient_id, end_date=None)

        with app_module.app.app_context():
            req = app_module.RefillRequest(medicine_id=med_id, patient_id=patient_id, doctor_id=doctor_id, status='denied')
            app_module.db.session.add(req)
            app_module.db.session.commit()
            req_id = req.id

        login(client, 'doc@example.com', 'password123')
        client.post(f'/refill-request/{req_id}/respond', data={'action': 'approve'}, follow_redirects=True)

        with app_module.app.app_context():
            med = app_module.db.session.get(app_module.Medicine, med_id)
            assert med.end_date is None  # unaffected - request was already resolved

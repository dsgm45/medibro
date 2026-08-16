"""
Tests for patient self-service account deletion: request -> lock -> admin
approval -> scheduled execution -> full cascade deletion. Given the real
stakes here (genuinely irreversible data deletion, and an account-locking
mechanism that could accidentally lock out every patient if built wrong),
this file is deliberately thorough.
"""
import app as app_module
from conftest import login


class TestRequestDeletion:
    def test_request_creates_pending_record_with_7_day_schedule(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        client.post('/profile/request-deletion', follow_redirects=True)

        with app_module.app.app_context():
            req = app_module.AccountDeletionRequest.query.filter_by(patient_id=patient_id).first()
            assert req is not None
            assert req.status == 'pending'
            days_diff = (req.scheduled_for - req.requested_at).days
            assert days_diff == 7

    def test_request_notifies_every_admin(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        admin1_id = make_user('admin1@example.com', 'password123', role='hospital', status='approved')
        admin2_id = make_user('admin2@example.com', 'password123', role='hospital', status='approved')
        login(client, 'patient@example.com', 'password123')

        client.post('/profile/request-deletion', follow_redirects=True)

        with app_module.app.app_context():
            notif1 = app_module.Notification.query.filter_by(user_id=admin1_id, type='account_deletion_requested').first()
            notif2 = app_module.Notification.query.filter_by(user_id=admin2_id, type='account_deletion_requested').first()
            assert notif1 is not None
            assert notif2 is not None

    def test_duplicate_request_does_not_create_a_second_record(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        client.post('/profile/request-deletion', follow_redirects=True)
        client.post('/profile/request-deletion', follow_redirects=True)

        with app_module.app.app_context():
            count = app_module.AccountDeletionRequest.query.filter_by(patient_id=patient_id).count()
            assert count == 1


class TestAccountLocking:
    def test_pending_request_locks_out_other_pages(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        client.post('/profile/request-deletion', follow_redirects=True)

        resp = client.get('/my-health', follow_redirects=False)
        assert resp.status_code == 302
        assert '/account-locked' in resp.location

    def test_locked_patient_can_still_reach_the_locked_page_itself(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        client.post('/profile/request-deletion', follow_redirects=True)

        resp = client.get('/account-locked')
        assert resp.status_code == 200
        assert b'Account Deletion Requested' in resp.data

    def test_locked_patient_can_still_logout(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        client.post('/profile/request-deletion', follow_redirects=True)

        resp = client.post('/logout', follow_redirects=False)
        assert resp.status_code == 302
        assert '/account-locked' not in resp.location

    def test_doctor_never_locked_regardless_of_any_patient_deletion_status(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'patient@example.com', 'password123')
        client.post('/profile/request-deletion', follow_redirects=True)
        client.post('/logout', follow_redirects=True)

        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor')
        assert resp.status_code == 200
        assert b'Account Deletion Requested' not in resp.data

    def test_admin_never_locked_regardless_of_any_patient_deletion_status(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'patient@example.com', 'password123')
        client.post('/profile/request-deletion', follow_redirects=True)
        client.post('/logout', follow_redirects=True)

        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin')
        assert resp.status_code == 200

    def test_patient_without_any_deletion_request_is_never_locked(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.get('/my-health')
        assert resp.status_code == 200

    def test_cancelling_unlocks_the_account(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        client.post('/profile/request-deletion', follow_redirects=True)

        with app_module.app.app_context():
            req_id = app_module.AccountDeletionRequest.query.first().id

        client.post(f'/account-deletion/{req_id}/cancel', follow_redirects=True)

        resp = client.get('/my-health')
        assert resp.status_code == 200


class TestAdminApproval:
    def test_admin_can_approve_pending_request(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        admin_id = make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'patient@example.com', 'password123')
        client.post('/profile/request-deletion', follow_redirects=True)
        client.post('/logout', follow_redirects=True)

        with app_module.app.app_context():
            req_id = app_module.AccountDeletionRequest.query.first().id

        login(client, 'admin@example.com', 'password123')
        client.post(f'/account-deletion/{req_id}/approve', follow_redirects=True)

        with app_module.app.app_context():
            req = app_module.db.session.get(app_module.AccountDeletionRequest, req_id)
            assert req.status == 'approved'
            assert req.approved_by_id == admin_id
            assert req.approved_at is not None

    def test_admin_can_cancel_pending_request(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'patient@example.com', 'password123')
        client.post('/profile/request-deletion', follow_redirects=True)
        client.post('/logout', follow_redirects=True)

        with app_module.app.app_context():
            req_id = app_module.AccountDeletionRequest.query.first().id

        login(client, 'admin@example.com', 'password123')
        client.post(f'/account-deletion/{req_id}/cancel', follow_redirects=True)

        with app_module.app.app_context():
            req = app_module.db.session.get(app_module.AccountDeletionRequest, req_id)
            assert req.status == 'cancelled'

    def test_doctor_cannot_approve(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'patient@example.com', 'password123')
        client.post('/profile/request-deletion', follow_redirects=True)
        client.post('/logout', follow_redirects=True)

        with app_module.app.app_context():
            req_id = app_module.AccountDeletionRequest.query.first().id

        login(client, 'doc@example.com', 'password123')
        resp = client.post(f'/account-deletion/{req_id}/approve', follow_redirects=False)
        assert resp.status_code == 302

        with app_module.app.app_context():
            req = app_module.db.session.get(app_module.AccountDeletionRequest, req_id)
            assert req.status == 'pending'  # unchanged


class TestExecuteDueAccountDeletions:
    def _make_pending_or_approved_request(self, patient_id, status, scheduled_for, approved_by_id=None):
        with app_module.app.app_context():
            req = app_module.AccountDeletionRequest(
                patient_id=patient_id,
                requested_at=scheduled_for - app_module.timedelta(days=7),
                scheduled_for=scheduled_for,
                status=status,
                approved_by_id=approved_by_id,
                approved_at=app_module.datetime.utcnow() if status == 'approved' else None
            )
            app_module.db.session.add(req)
            app_module.db.session.commit()
            return req.id

    def test_overdue_but_unapproved_request_is_never_deleted(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        overdue_date = app_module.datetime.utcnow() - app_module.timedelta(days=3)
        self._make_pending_or_approved_request(patient_id, 'pending', overdue_date)

        with app_module.app.app_context():
            app_module.execute_due_account_deletions()
            user_still_exists = app_module.db.session.get(app_module.User, patient_id)
            assert user_still_exists is not None

    def test_approved_and_overdue_request_gets_executed(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        admin_id = make_user('admin@example.com', 'password123', role='hospital', status='approved')
        overdue_date = app_module.datetime.utcnow() - app_module.timedelta(days=1)
        self._make_pending_or_approved_request(patient_id, 'approved', overdue_date, approved_by_id=admin_id)

        with app_module.app.app_context():
            app_module.execute_due_account_deletions()
            user_gone = app_module.db.session.get(app_module.User, patient_id)
            assert user_gone is None

    def test_approved_but_not_yet_due_request_is_not_executed(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        admin_id = make_user('admin@example.com', 'password123', role='hospital', status='approved')
        future_date = app_module.datetime.utcnow() + app_module.timedelta(days=3)
        self._make_pending_or_approved_request(patient_id, 'approved', future_date, approved_by_id=admin_id)

        with app_module.app.app_context():
            app_module.execute_due_account_deletions()
            user_still_exists = app_module.db.session.get(app_module.User, patient_id)
            assert user_still_exists is not None

    def test_execution_creates_audit_log_entry(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        admin_id = make_user('admin@example.com', 'password123', role='hospital', status='approved')
        overdue_date = app_module.datetime.utcnow() - app_module.timedelta(days=1)
        self._make_pending_or_approved_request(patient_id, 'approved', overdue_date, approved_by_id=admin_id)

        with app_module.app.app_context():
            app_module.execute_due_account_deletions()
            audit = app_module.AdminAuditLog.query.filter_by(action='account_deletion_executed').first()
            assert audit is not None
            assert audit.admin_id == admin_id


class TestCascadeDeletion:
    def test_deletion_removes_everything_tied_to_the_patient(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')

        with app_module.app.app_context():
            appt = app_module.Appointment(
                patient_id=patient_id, doctor_id=doctor_id,
                appointment_date='2026-08-01', appointment_time='10:00',
                phone_number='555', status='completed'
            )
            app_module.db.session.add(appt)
            app_module.db.session.flush()

            app_module.db.session.add(app_module.Message(appointment_id=appt.id, sender_id=patient_id, content='hi'))
            app_module.db.session.add(app_module.Vital(patient_id=patient_id, heart_rate=72))
            app_module.db.session.add(app_module.SymptomLog(patient_id=patient_id, symptoms='Headache'))
            app_module.db.session.add(app_module.EmergencyContact(patient_id=patient_id, contact_name='Mom', contact_phone='555'))
            app_module.db.session.add(app_module.SosEvent(patient_id=patient_id, notes='test'))
            app_module.db.session.add(app_module.AIChatMessage(patient_id=patient_id, sender='patient', content='hello'))
            app_module.db.session.add(app_module.Document(patient_id=patient_id, original_filename='x.pdf', file_data=b'data', file_size=4))
            app_module.db.session.add(app_module.Notification(user_id=patient_id, type='medicine_reminder', message='test'))

            med = app_module.Medicine(patient_id=patient_id, name='Metformin')
            app_module.db.session.add(med)
            app_module.db.session.flush()
            dose = app_module.MedicineDose(medicine_id=med.id, time='08:00')
            app_module.db.session.add(dose)
            app_module.db.session.flush()
            app_module.db.session.add(app_module.MedicineDoseLog(dose_id=dose.id, log_date=app_module.get_ist_today()))

            db_appt_id = appt.id
            db_med_id = med.id
            db_dose_id = dose.id

            app_module.db.session.commit()

        with app_module.app.app_context():
            result = app_module._permanently_delete_patient_account(patient_id)
            assert result is True

            assert app_module.db.session.get(app_module.User, patient_id) is None
            assert app_module.Appointment.query.filter_by(patient_id=patient_id).count() == 0
            assert app_module.Message.query.filter_by(appointment_id=db_appt_id).count() == 0
            assert app_module.Vital.query.filter_by(patient_id=patient_id).count() == 0
            assert app_module.SymptomLog.query.filter_by(patient_id=patient_id).count() == 0
            assert app_module.EmergencyContact.query.filter_by(patient_id=patient_id).count() == 0
            assert app_module.SosEvent.query.filter_by(patient_id=patient_id).count() == 0
            assert app_module.AIChatMessage.query.filter_by(patient_id=patient_id).count() == 0
            assert app_module.Document.query.filter_by(patient_id=patient_id).count() == 0
            assert app_module.Notification.query.filter_by(user_id=patient_id).count() == 0
            assert app_module.Medicine.query.filter_by(patient_id=patient_id).count() == 0
            assert app_module.MedicineDose.query.filter_by(id=db_dose_id).count() == 0
            assert app_module.MedicineDoseLog.query.filter_by(dose_id=db_dose_id).count() == 0

            # The doctor themselves must be completely unaffected
            doctor_still_exists = app_module.db.session.get(app_module.User, doctor_id)
            assert doctor_still_exists is not None

    def test_deletion_preserves_other_peoples_own_notifications(self, client, make_user):
        # A doctor's notification ABOUT this patient (e.g. "patient accepted
        # your follow-up") has user_id = the doctor, not the patient - it
        # must survive the patient's deletion, since it's the doctor's own
        # record, not the departed patient's.
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')

        with app_module.app.app_context():
            notif = app_module.Notification(
                user_id=doctor_id, type='follow_up_accepted',
                message=f'Patient {patient_id} accepted your follow-up.'
            )
            app_module.db.session.add(notif)
            app_module.db.session.commit()
            notif_id = notif.id

        with app_module.app.app_context():
            app_module._permanently_delete_patient_account(patient_id)
            still_there = app_module.db.session.get(app_module.Notification, notif_id)
            assert still_there is not None

    def test_cannot_delete_a_non_patient_account(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')

        with app_module.app.app_context():
            result = app_module._permanently_delete_patient_account(doctor_id)
            assert result is False
            still_exists = app_module.db.session.get(app_module.User, doctor_id)
            assert still_exists is not None

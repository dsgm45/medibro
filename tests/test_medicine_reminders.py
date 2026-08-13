"""
Tests for medicine reminders via the shared notification system.

Dose times are set to the extremes of the day (00:01 for "definitely due
already today" and 23:59 for "definitely not due yet today") rather than
mocking datetime.utcnow() - this reliably reflects due/not-due relative
to whenever the test actually runs, with only a negligible flakiness risk
right at literal midnight.
"""
import app as app_module
from conftest import login


def _setup_medicine_with_dose(patient_id, dose_time):
    with app_module.app.app_context():
        med = app_module.Medicine(patient_id=patient_id, name='Metformin', dosage='500mg')
        app_module.db.session.add(med)
        app_module.db.session.flush()
        dose = app_module.MedicineDose(medicine_id=med.id, time=dose_time)
        app_module.db.session.add(dose)
        app_module.db.session.commit()
        return med.id, dose.id


class TestReminderCreation:
    def test_due_untaken_dose_creates_notification(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        med_id, dose_id = _setup_medicine_with_dose(patient_id, '00:01')
        login(client, 'patient@example.com', 'password123')

        client.get('/my-health')  # triggers the context processor check

        with app_module.app.app_context():
            notif = app_module.Notification.query.filter_by(
                user_id=patient_id, type='medicine_reminder', related_id=dose_id
            ).first()
            assert notif is not None
            assert notif.is_read is False
            assert 'Metformin' in notif.message

    def test_not_yet_due_dose_creates_no_notification(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        med_id, dose_id = _setup_medicine_with_dose(patient_id, '23:59')
        login(client, 'patient@example.com', 'password123')

        client.get('/my-health')

        with app_module.app.app_context():
            notif = app_module.Notification.query.filter_by(
                user_id=patient_id, type='medicine_reminder', related_id=dose_id
            ).first()
            assert notif is None

    def test_already_taken_dose_creates_no_notification(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        med_id, dose_id = _setup_medicine_with_dose(patient_id, '00:01')
        with app_module.app.app_context():
            today = app_module.datetime.utcnow().date()
            log = app_module.MedicineDoseLog(dose_id=dose_id, log_date=today, taken_at=app_module.datetime.utcnow())
            app_module.db.session.add(log)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        client.get('/my-health')

        with app_module.app.app_context():
            notif = app_module.Notification.query.filter_by(
                user_id=patient_id, type='medicine_reminder', related_id=dose_id
            ).first()
            assert notif is None

    def test_repeated_visits_do_not_duplicate_notification(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        med_id, dose_id = _setup_medicine_with_dose(patient_id, '00:01')
        login(client, 'patient@example.com', 'password123')

        client.get('/my-health')
        client.get('/my-health')
        client.get('/vitals')

        with app_module.app.app_context():
            count = app_module.Notification.query.filter_by(
                user_id=patient_id, type='medicine_reminder', related_id=dose_id
            ).count()
            assert count == 1


class TestReminderClearingOnDoseTaken:
    def test_marking_dose_taken_marks_reminder_read(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        med_id, dose_id = _setup_medicine_with_dose(patient_id, '00:01')
        login(client, 'patient@example.com', 'password123')

        client.get('/my-health')  # creates the reminder, unread
        client.post(f'/medicines/dose/{dose_id}/toggle-taken', follow_redirects=True)

        with app_module.app.app_context():
            notif = app_module.Notification.query.filter_by(
                user_id=patient_id, type='medicine_reminder', related_id=dose_id
            ).first()
            assert notif is not None
            assert notif.is_read is True

    def test_untoggling_taken_marks_reminder_unread_again(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        med_id, dose_id = _setup_medicine_with_dose(patient_id, '00:01')
        login(client, 'patient@example.com', 'password123')

        client.get('/my-health')
        client.post(f'/medicines/dose/{dose_id}/toggle-taken', follow_redirects=True)  # mark taken
        client.post(f'/medicines/dose/{dose_id}/toggle-taken', follow_redirects=True)  # un-mark

        with app_module.app.app_context():
            notif = app_module.Notification.query.filter_by(
                user_id=patient_id, type='medicine_reminder', related_id=dose_id
            ).first()
            assert notif.is_read is False

    def test_other_patient_cannot_toggle_dose(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        med_id, dose_id = _setup_medicine_with_dose(patient_id, '00:01')
        make_user('otherpatient@example.com', 'password123', role='patient')
        login(client, 'otherpatient@example.com', 'password123')

        client.post(f'/medicines/dose/{dose_id}/toggle-taken', follow_redirects=True)

        with app_module.app.app_context():
            log = app_module.MedicineDoseLog.query.filter_by(dose_id=dose_id).first()
            assert log is None  # unauthorized attempt had no effect

"""
Tests for the redesigned follow-up flow (doctor proposes a specific
date/time, patient accepts/rejects) and the shared notification system
(bell icon) it introduces.
"""
import app as app_module
from conftest import login


def _complete_appointment_setup(make_user):
    """Creates a doctor, patient, and a completed appointment between
    them - the starting state needed before a follow-up can be proposed."""
    doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
    patient_id = make_user('patient@example.com', 'password123', role='patient')
    with app_module.app.app_context():
        appt = app_module.Appointment(
            patient_id=patient_id, doctor_id=doctor_id,
            appointment_date='2026-08-01', appointment_time='10:00',
            phone_number='555-0000', status='completed'
        )
        app_module.db.session.add(appt)
        app_module.db.session.commit()
        appt_id = appt.id
    return doctor_id, patient_id, appt_id


class TestProposeFollowUp:
    def test_doctor_can_propose_follow_up(self, client, make_user):
        doctor_id, patient_id, appt_id = _complete_appointment_setup(make_user)
        login(client, 'doc@example.com', 'password123')

        client.post(f'/appointment/{appt_id}/request-follow-up', data={
            'proposed_date': '2026-09-01', 'proposed_time': '14:00'
        }, follow_redirects=True)

        with app_module.app.app_context():
            fu = app_module.FollowUpRequest.query.filter_by(original_appointment_id=appt_id).first()
            assert fu is not None
            assert fu.status == 'pending'
            assert fu.proposed_date == '2026-09-01'
            assert fu.proposed_time == '14:00'

    def test_cannot_propose_without_date_and_time(self, client, make_user):
        doctor_id, patient_id, appt_id = _complete_appointment_setup(make_user)
        login(client, 'doc@example.com', 'password123')

        client.post(f'/appointment/{appt_id}/request-follow-up', data={
            'proposed_date': '', 'proposed_time': ''
        }, follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.FollowUpRequest.query.count() == 0

    def test_cannot_propose_second_pending_for_same_visit(self, client, make_user):
        doctor_id, patient_id, appt_id = _complete_appointment_setup(make_user)
        login(client, 'doc@example.com', 'password123')

        client.post(f'/appointment/{appt_id}/request-follow-up', data={
            'proposed_date': '2026-09-01', 'proposed_time': '14:00'
        }, follow_redirects=True)
        client.post(f'/appointment/{appt_id}/request-follow-up', data={
            'proposed_date': '2026-09-05', 'proposed_time': '15:00'
        }, follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.FollowUpRequest.query.filter_by(original_appointment_id=appt_id).count() == 1

    def test_other_doctor_cannot_propose_for_this_appointment(self, client, make_user):
        doctor_id, patient_id, appt_id = _complete_appointment_setup(make_user)
        make_user('otherdoc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'otherdoc@example.com', 'password123')

        client.post(f'/appointment/{appt_id}/request-follow-up', data={
            'proposed_date': '2026-09-01', 'proposed_time': '14:00'
        }, follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.FollowUpRequest.query.count() == 0


class TestAcceptFollowUp:
    def test_accept_creates_real_appointment_and_notifies_doctor(self, client, make_user):
        doctor_id, patient_id, appt_id = _complete_appointment_setup(make_user)
        with app_module.app.app_context():
            fu = app_module.FollowUpRequest(
                original_appointment_id=appt_id, doctor_id=doctor_id, patient_id=patient_id,
                proposed_date='2026-09-01', proposed_time='14:00', status='pending'
            )
            app_module.db.session.add(fu)
            app_module.db.session.commit()
            fu_id = fu.id

        login(client, 'patient@example.com', 'password123')
        client.post(f'/follow-up/{fu_id}/accept', follow_redirects=True)

        with app_module.app.app_context():
            fu = app_module.db.session.get(app_module.FollowUpRequest, fu_id)
            assert fu.status == 'accepted'
            assert fu.resulting_appointment_id is not None

            new_appt = app_module.db.session.get(app_module.Appointment, fu.resulting_appointment_id)
            assert new_appt.status == 'accepted'
            assert new_appt.appointment_date == '2026-09-01'
            assert new_appt.appointment_time == '14:00'

            notif = app_module.Notification.query.filter_by(user_id=doctor_id, type='follow_up_accepted').first()
            assert notif is not None
            assert notif.is_read is False

    def test_accept_respects_double_booking(self, client, make_user):
        doctor_id, patient_id, appt_id = _complete_appointment_setup(make_user)
        with app_module.app.app_context():
            # An existing accepted appointment already occupies the slot
            conflict = app_module.Appointment(
                patient_id=patient_id, doctor_id=doctor_id,
                appointment_date='2026-09-01', appointment_time='14:00',
                phone_number='555', status='accepted'
            )
            app_module.db.session.add(conflict)
            fu = app_module.FollowUpRequest(
                original_appointment_id=appt_id, doctor_id=doctor_id, patient_id=patient_id,
                proposed_date='2026-09-01', proposed_time='14:00', status='pending'
            )
            app_module.db.session.add(fu)
            app_module.db.session.commit()
            fu_id = fu.id

        login(client, 'patient@example.com', 'password123')
        client.post(f'/follow-up/{fu_id}/accept', follow_redirects=True)

        with app_module.app.app_context():
            fu = app_module.db.session.get(app_module.FollowUpRequest, fu_id)
            assert fu.status == 'pending'  # unchanged - conflict blocked it
            assert fu.resulting_appointment_id is None

    def test_other_patient_cannot_accept(self, client, make_user):
        doctor_id, patient_id, appt_id = _complete_appointment_setup(make_user)
        make_user('otherpatient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            fu = app_module.FollowUpRequest(
                original_appointment_id=appt_id, doctor_id=doctor_id, patient_id=patient_id,
                proposed_date='2026-09-01', proposed_time='14:00', status='pending'
            )
            app_module.db.session.add(fu)
            app_module.db.session.commit()
            fu_id = fu.id

        login(client, 'otherpatient@example.com', 'password123')
        client.post(f'/follow-up/{fu_id}/accept', follow_redirects=True)

        with app_module.app.app_context():
            fu = app_module.db.session.get(app_module.FollowUpRequest, fu_id)
            assert fu.status == 'pending'  # unchanged - unauthorized

    def test_cannot_accept_already_responded_request(self, client, make_user):
        doctor_id, patient_id, appt_id = _complete_appointment_setup(make_user)
        with app_module.app.app_context():
            fu = app_module.FollowUpRequest(
                original_appointment_id=appt_id, doctor_id=doctor_id, patient_id=patient_id,
                proposed_date='2026-09-01', proposed_time='14:00', status='rejected'
            )
            app_module.db.session.add(fu)
            app_module.db.session.commit()
            fu_id = fu.id

        login(client, 'patient@example.com', 'password123')
        client.post(f'/follow-up/{fu_id}/accept', follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.Appointment.query.filter_by(reason='Follow-up visit').count() == 0


class TestRejectFollowUp:
    def test_reject_notifies_doctor_and_creates_no_appointment(self, client, make_user):
        doctor_id, patient_id, appt_id = _complete_appointment_setup(make_user)
        with app_module.app.app_context():
            fu = app_module.FollowUpRequest(
                original_appointment_id=appt_id, doctor_id=doctor_id, patient_id=patient_id,
                proposed_date='2026-09-01', proposed_time='14:00', status='pending'
            )
            app_module.db.session.add(fu)
            app_module.db.session.commit()
            fu_id = fu.id

        login(client, 'patient@example.com', 'password123')
        client.post(f'/follow-up/{fu_id}/reject', follow_redirects=True)

        with app_module.app.app_context():
            fu = app_module.db.session.get(app_module.FollowUpRequest, fu_id)
            assert fu.status == 'rejected'

            notif = app_module.Notification.query.filter_by(user_id=doctor_id, type='follow_up_rejected').first()
            assert notif is not None

            assert app_module.Appointment.query.filter_by(reason='Follow-up visit').count() == 0


class TestNotifications:
    def test_unread_count_shows_in_context(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        with app_module.app.app_context():
            n = app_module.Notification(user_id=doctor_id, type='follow_up_accepted', message='Test', is_read=False)
            app_module.db.session.add(n)
            app_module.db.session.commit()

        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor')
        assert b'>1</span>' in resp.data
        assert b'9+' not in resp.data

    def test_visiting_notifications_page_marks_as_read(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        with app_module.app.app_context():
            n = app_module.Notification(user_id=doctor_id, type='follow_up_accepted', message='Test', is_read=False)
            app_module.db.session.add(n)
            app_module.db.session.commit()
            n_id = n.id

        login(client, 'doc@example.com', 'password123')
        client.get('/notifications')

        with app_module.app.app_context():
            n = app_module.db.session.get(app_module.Notification, n_id)
            assert n.is_read is True

    def test_medicine_reminder_type_not_marked_read_on_view(self, client, make_user):
        # Forward-looking test: medicine reminders aren't built yet, but
        # this locks in that visiting /notifications must NOT mark that
        # type as read, since it needs to persist until the dose itself
        # is marked taken, not just until viewed.
        patient_id = make_user('patient2@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            n = app_module.Notification(user_id=patient_id, type='medicine_reminder', message='Take your pill', is_read=False)
            app_module.db.session.add(n)
            app_module.db.session.commit()
            n_id = n.id

        login(client, 'patient2@example.com', 'password123')
        client.get('/notifications')

        with app_module.app.app_context():
            n = app_module.db.session.get(app_module.Notification, n_id)
            assert n.is_read is False

    def test_notifications_requires_login(self, client):
        resp = client.get('/notifications', follow_redirects=False)
        assert resp.status_code == 302

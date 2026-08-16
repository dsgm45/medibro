"""
Tests for the notification retention policy: read notifications purge
automatically one day after being marked read (checked opportunistically
on page load), and the Clear All button deletes everything currently
read immediately. Unread medicine reminders are untouched by either
mechanism - only actually taking the dose (or the next-day stale
rollover) can mark those read.
"""
from datetime import datetime, timedelta
import app as app_module
from conftest import login


class TestAutomaticPurge:
    def test_read_notification_older_than_a_day_gets_purged_on_view(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            old_read = app_module.Notification(
                user_id=patient_id, type='follow_up_response', message='Old read one',
                is_read=True, read_at=datetime.utcnow() - timedelta(days=2)
            )
            app_module.db.session.add(old_read)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        client.get('/notifications')

        with app_module.app.app_context():
            assert app_module.Notification.query.filter_by(user_id=patient_id).count() == 0

    def test_read_notification_less_than_a_day_old_survives(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            recent_read = app_module.Notification(
                user_id=patient_id, type='follow_up_response', message='Recently read',
                is_read=True, read_at=datetime.utcnow() - timedelta(hours=2)
            )
            app_module.db.session.add(recent_read)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        client.get('/notifications')

        with app_module.app.app_context():
            assert app_module.Notification.query.filter_by(user_id=patient_id).count() == 1

    def test_unread_notification_never_purged_regardless_of_age(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            old_unread = app_module.Notification(
                user_id=patient_id, type='medicine_reminder', message='Old unread reminder',
                is_read=False, read_at=None,
                created_at=datetime.utcnow() - timedelta(days=10)
            )
            app_module.db.session.add(old_unread)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        client.get('/notifications')

        with app_module.app.app_context():
            assert app_module.Notification.query.filter_by(user_id=patient_id).count() == 1


class TestClearAllButton:
    def test_clear_all_deletes_read_notifications_immediately(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            recent_read = app_module.Notification(
                user_id=patient_id, type='follow_up_response', message='Read just now',
                is_read=True, read_at=datetime.utcnow()
            )
            app_module.db.session.add(recent_read)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        client.post('/notifications/clear-all', follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.Notification.query.filter_by(user_id=patient_id).count() == 0

    def test_clear_all_never_touches_unread_medicine_reminders(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            unread_reminder = app_module.Notification(
                user_id=patient_id, type='medicine_reminder', message='Take your medicine',
                is_read=False
            )
            app_module.db.session.add(unread_reminder)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        client.post('/notifications/clear-all', follow_redirects=True)

        with app_module.app.app_context():
            remaining = app_module.Notification.query.filter_by(user_id=patient_id).first()
            assert remaining is not None
            assert remaining.is_read is False

    def test_clear_all_leaves_other_users_notifications_untouched(self, client, make_user):
        patient_a_id = make_user('patienta@example.com', 'password123', role='patient')
        patient_b_id = make_user('patientb@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            b_notification = app_module.Notification(
                user_id=patient_b_id, type='follow_up_response', message='Belongs to B',
                is_read=True, read_at=datetime.utcnow()
            )
            app_module.db.session.add(b_notification)
            app_module.db.session.commit()

        login(client, 'patienta@example.com', 'password123')
        client.post('/notifications/clear-all', follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.Notification.query.filter_by(user_id=patient_b_id).count() == 1

    def test_clear_all_requires_login(self, client):
        resp = client.post('/notifications/clear-all', follow_redirects=False)
        assert resp.status_code == 302


class TestGoBackLink:
    def test_notifications_page_has_go_back_link(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/notifications')
        assert b'Go Back' in resp.data
        assert b'history.back()' in resp.data

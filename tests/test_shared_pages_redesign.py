"""
Tests for the high-traffic shared pages redesign: Notifications, Chat
List, Chat Thread, and Access Log. Unread notifications now use the
marigold accent (needs-attention meaning), consistent with the rest
of the app's color-by-meaning scheme, replacing the old blue.
"""
from datetime import datetime
import app as app_module
from conftest import login


class TestNotificationsRedesign:
    def test_no_hardcoded_light_mode_colors(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/notifications')
        text = resp.data.decode()
        assert '#0f172a' not in text
        assert '#2563eb' not in text
        assert '#eff6ff' not in text

    def test_unread_notification_uses_accent_light(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            notif = app_module.Notification(user_id=patient_id, type='medicine_reminder', message='Time for Metformin', is_read=False, created_at=datetime.utcnow())
            app_module.db.session.add(notif)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/notifications')
        text = resp.data.decode()
        assert 'var(--accent-light)' in text

    def test_empty_state(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/notifications')
        assert b'No notifications yet' in resp.data


class TestChatListRedesign:
    def test_no_hardcoded_colors(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/chat')
        text = resp.data.decode()
        assert '#f1f5f9' not in text

    def test_empty_state(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/chat')
        assert b'No conversations yet' in resp.data


class TestChatThreadRedesign:
    def test_no_hardcoded_message_bubble_color(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        with app_module.app.app_context():
            appt = app_module.Appointment(patient_id=patient_id, doctor_id=doctor_id, appointment_date='2026-08-01', phone_number='555', status='accepted')
            app_module.db.session.add(appt)
            app_module.db.session.commit()
            appt_id = appt.id

        login(client, 'patient@example.com', 'password123')
        resp = client.get(f'/chat/{appt_id}')
        text = resp.data.decode()
        assert '#f1f5f9' not in text

    def test_send_message_form_present(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        with app_module.app.app_context():
            appt = app_module.Appointment(patient_id=patient_id, doctor_id=doctor_id, appointment_date='2026-08-01', phone_number='555', status='accepted')
            app_module.db.session.add(appt)
            app_module.db.session.commit()
            appt_id = appt.id

        login(client, 'patient@example.com', 'password123')
        resp = client.get(f'/chat/{appt_id}')
        assert b'name="content"' in resp.data


class TestAccessLogRedesign:
    def test_no_hardcoded_colors(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health/access-log')
        text = resp.data.decode()
        assert '#0f172a' not in text
        assert '#64748b' not in text

    def test_empty_state(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health/access-log')
        assert b'No one has viewed your records yet' in resp.data

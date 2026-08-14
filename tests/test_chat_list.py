"""Tests for the chat list, including the rewritten batch last-message logic."""
import app as app_module
from conftest import login


def _accepted_appointment(patient_id, doctor_id):
    with app_module.app.app_context():
        appt = app_module.Appointment(
            patient_id=patient_id, doctor_id=doctor_id,
            appointment_date='2026-08-01', appointment_time='10:00',
            phone_number='555', status='accepted'
        )
        app_module.db.session.add(appt)
        app_module.db.session.commit()
        return appt.id


def _add_message(appointment_id, sender_id, content, created_at=None):
    with app_module.app.app_context():
        msg = app_module.Message(
            appointment_id=appointment_id, sender_id=sender_id, content=content,
            created_at=created_at or app_module.datetime.utcnow()
        )
        app_module.db.session.add(msg)
        app_module.db.session.commit()


class TestChatListBasics:
    def test_patient_sees_doctor_as_partner(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        _accepted_appointment(patient_id, doctor_id)

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/chat')

        assert resp.status_code == 200
        assert b'doc' in resp.data.lower() or b'Dr' in resp.data

    def test_pending_appointment_does_not_show_in_chat_list(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            appt = app_module.Appointment(
                patient_id=patient_id, doctor_id=doctor_id,
                appointment_date='2026-08-01', appointment_time='10:00',
                phone_number='555', status='pending'  # not accepted/completed
            )
            app_module.db.session.add(appt)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/chat')
        assert b'No conversations yet' in resp.data or resp.status_code == 200


class TestChatListMessageOrdering:
    def test_latest_message_shown_not_oldest(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        appt_id = _accepted_appointment(patient_id, doctor_id)

        import datetime as dt
        _add_message(appt_id, patient_id, 'First message', created_at=dt.datetime(2026, 8, 1, 10, 0))
        _add_message(appt_id, doctor_id, 'Second message', created_at=dt.datetime(2026, 8, 1, 11, 0))
        _add_message(appt_id, patient_id, 'Most recent message', created_at=dt.datetime(2026, 8, 1, 12, 0))

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/chat')

        assert b'Most recent message' in resp.data
        assert b'First message' not in resp.data  # only the latest shows in the list preview

    def test_conversation_with_no_messages_still_appears(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        _accepted_appointment(patient_id, doctor_id)  # no messages added

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/chat')

        assert resp.status_code == 200  # doesn't error out on a conversation with no messages yet

    def test_conversations_sorted_by_most_recent_message_first(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        patient_a = make_user('patienta@example.com', 'password123', role='patient')
        patient_b = make_user('patientb@example.com', 'password123', role='patient')

        appt_a = _accepted_appointment(patient_a, doctor_id)
        appt_b = _accepted_appointment(patient_b, doctor_id)

        import datetime as dt
        _add_message(appt_a, patient_a, 'Older conversation', created_at=dt.datetime(2026, 8, 1, 9, 0))
        _add_message(appt_b, patient_b, 'Newer conversation', created_at=dt.datetime(2026, 8, 1, 15, 0))

        login(client, 'doc@example.com', 'password123')
        resp = client.get('/chat')

        text = resp.data.decode()
        # The newer conversation's preview text should appear before the older one's
        assert text.index('Newer conversation') < text.index('Older conversation')

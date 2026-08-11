"""Tests for appointment booking, including the double-booking prevention logic."""
import app as app_module
from conftest import login


def _book(client, doctor_id, date='2026-12-01', time='10:00', phone='555-1234', reason='Checkup'):
    return client.post('/book-appointment', data={
        'doctor_id': str(doctor_id),
        'appointment_date': date,
        'appointment_time': time,
        'phone_number': phone,
        'reason': reason,
    }, follow_redirects=True)


class TestBooking:
    def test_patient_can_book_appointment(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved', specialty='Cardiology')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        _book(client, doctor_id)

        with app_module.app.app_context():
            appts = app_module.Appointment.query.all()
            assert len(appts) == 1
            assert appts[0].status == 'pending'
            assert appts[0].doctor_id == doctor_id

    def test_booking_requires_phone_number(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        client.post('/book-appointment', data={
            'doctor_id': str(doctor_id),
            'appointment_date': '2026-12-01',
            'appointment_time': '10:00',
            'phone_number': '',
            'reason': 'Checkup',
        }, follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.Appointment.query.count() == 0

    def test_booking_requires_date_and_time(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        client.post('/book-appointment', data={
            'doctor_id': str(doctor_id),
            'appointment_date': '',
            'appointment_time': '',
            'phone_number': '555-1234',
        }, follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.Appointment.query.count() == 0

    def test_booking_rejects_malformed_doctor_id(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.post('/book-appointment', data={
            'doctor_id': 'not-a-number',
            'appointment_date': '2026-12-01',
            'appointment_time': '10:00',
            'phone_number': '555-1234',
        }, follow_redirects=True)

        # Should be handled gracefully (redirect with flash), not a 500 crash
        assert resp.status_code == 200
        with app_module.app.app_context():
            assert app_module.Appointment.query.count() == 0


class TestDoubleBookingPrevention:
    def test_same_doctor_same_slot_is_rejected(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient_a@example.com', 'password123', role='patient')
        make_user('patient_b@example.com', 'password123', role='patient')

        login(client, 'patient_a@example.com', 'password123')
        _book(client, doctor_id, date='2026-12-05', time='14:00')

        client.post('/logout', follow_redirects=True)
        login(client, 'patient_b@example.com', 'password123')
        _book(client, doctor_id, date='2026-12-05', time='14:00')

        with app_module.app.app_context():
            appts = app_module.Appointment.query.filter_by(
                doctor_id=doctor_id, appointment_date='2026-12-05', appointment_time='14:00'
            ).all()
            assert len(appts) == 1  # second booking attempt was rejected

    def test_different_time_same_doctor_is_allowed(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        _book(client, doctor_id, date='2026-12-05', time='09:00')
        _book(client, doctor_id, date='2026-12-05', time='10:00')

        with app_module.app.app_context():
            assert app_module.Appointment.query.filter_by(doctor_id=doctor_id).count() == 2

    def test_different_doctor_same_slot_is_allowed(self, client, make_user):
        doctor_a_id = make_user('doca@example.com', 'password123', role='doctor', status='approved')
        doctor_b_id = make_user('docb@example.com', 'password123', role='doctor', status='approved')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        _book(client, doctor_a_id, date='2026-12-05', time='10:00')
        _book(client, doctor_b_id, date='2026-12-05', time='10:00')

        with app_module.app.app_context():
            assert app_module.Appointment.query.count() == 2

    def test_declined_appointment_frees_the_slot(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient_a@example.com', 'password123', role='patient')
        make_user('patient_b@example.com', 'password123', role='patient')

        login(client, 'patient_a@example.com', 'password123')
        _book(client, doctor_id, date='2026-12-05', time='11:00')

        with app_module.app.app_context():
            appt = app_module.Appointment.query.filter_by(doctor_id=doctor_id).first()
            appt.status = 'declined'
            app_module.db.session.commit()

        client.post('/logout', follow_redirects=True)
        login(client, 'patient_b@example.com', 'password123')
        _book(client, doctor_id, date='2026-12-05', time='11:00')

        with app_module.app.app_context():
            active = app_module.Appointment.query.filter_by(
                doctor_id=doctor_id, appointment_date='2026-12-05', appointment_time='11:00'
            ).filter(app_module.Appointment.status.in_(['pending', 'accepted'])).all()
            assert len(active) == 1  # patient B's new booking went through

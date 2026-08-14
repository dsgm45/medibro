"""
Tests for making appointment time optional at booking: a patient can
request just a date, and the doctor assigns the actual time when
accepting. The key invariant this relies on: an appointment can only
reach 'accepted' status once it has a real time.
"""
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


class TestBookingWithoutTime:
    def test_booking_with_blank_time_succeeds(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        _book(client, doctor_id, date='2026-12-05', time='')

        with app_module.app.app_context():
            appt = app_module.Appointment.query.filter_by(doctor_id=doctor_id).first()
            assert appt is not None
            assert appt.appointment_time is None
            assert appt.status == 'pending'

    def test_two_blank_time_requests_same_date_are_not_a_conflict(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient_a@example.com', 'password123', role='patient')
        make_user('patient_b@example.com', 'password123', role='patient')

        login(client, 'patient_a@example.com', 'password123')
        _book(client, doctor_id, date='2026-12-05', time='')

        client.post('/logout', follow_redirects=True)
        login(client, 'patient_b@example.com', 'password123')
        _book(client, doctor_id, date='2026-12-05', time='')

        with app_module.app.app_context():
            count = app_module.Appointment.query.filter_by(
                doctor_id=doctor_id, appointment_date='2026-12-05'
            ).count()
            assert count == 2  # both went through - no conflict since neither has a real time

    def test_blank_time_request_does_not_conflict_with_existing_specific_time(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient_a@example.com', 'password123', role='patient')
        make_user('patient_b@example.com', 'password123', role='patient')

        login(client, 'patient_a@example.com', 'password123')
        _book(client, doctor_id, date='2026-12-05', time='10:00')  # specific time

        client.post('/logout', follow_redirects=True)
        login(client, 'patient_b@example.com', 'password123')
        _book(client, doctor_id, date='2026-12-05', time='')  # blank time, same date

        with app_module.app.app_context():
            count = app_module.Appointment.query.filter_by(
                doctor_id=doctor_id, appointment_date='2026-12-05'
            ).count()
            assert count == 2  # both exist - the blank-time request isn't blocked


class TestDoctorAcceptingBlankTimeRequest:
    def _setup_pending_no_time(self, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            appt = app_module.Appointment(
                patient_id=patient_id, doctor_id=doctor_id,
                appointment_date='2026-12-05', appointment_time=None,
                phone_number='555-0000', status='pending'
            )
            app_module.db.session.add(appt)
            app_module.db.session.commit()
            appt_id = appt.id
        return doctor_id, patient_id, appt_id

    def test_accept_without_providing_time_fails(self, client, make_user):
        doctor_id, patient_id, appt_id = self._setup_pending_no_time(make_user)
        login(client, 'doc@example.com', 'password123')

        client.post(f'/appointment/{appt_id}/accept', data={}, follow_redirects=True)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            assert appt.status == 'pending'  # unchanged - no time provided
            assert appt.appointment_time is None

    def test_accept_with_time_succeeds_and_sets_it(self, client, make_user):
        doctor_id, patient_id, appt_id = self._setup_pending_no_time(make_user)
        login(client, 'doc@example.com', 'password123')

        client.post(f'/appointment/{appt_id}/accept', data={'assigned_time': '15:00'}, follow_redirects=True)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            assert appt.status == 'accepted'
            assert appt.appointment_time == '15:00'

    def test_accept_with_conflicting_time_is_blocked(self, client, make_user):
        doctor_id, patient_id, appt_id = self._setup_pending_no_time(make_user)
        with app_module.app.app_context():
            # A separate, already-accepted appointment occupies 15:00
            other = app_module.Appointment(
                patient_id=patient_id, doctor_id=doctor_id,
                appointment_date='2026-12-05', appointment_time='15:00',
                phone_number='555', status='accepted'
            )
            app_module.db.session.add(other)
            app_module.db.session.commit()

        login(client, 'doc@example.com', 'password123')
        client.post(f'/appointment/{appt_id}/accept', data={'assigned_time': '15:00'}, follow_redirects=True)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            assert appt.status == 'pending'  # blocked by the conflict
            assert appt.appointment_time is None

    def test_accept_with_time_already_set_ignores_assigned_time_field(self, client, make_user):
        # An appointment that already has a real time (patient specified
        # one at booking) should accept normally without needing
        # assigned_time at all - unchanged from the original behavior.
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            appt = app_module.Appointment(
                patient_id=patient_id, doctor_id=doctor_id,
                appointment_date='2026-12-05', appointment_time='09:00',
                phone_number='555', status='pending'
            )
            app_module.db.session.add(appt)
            app_module.db.session.commit()
            appt_id = appt.id

        login(client, 'doc@example.com', 'password123')
        client.post(f'/appointment/{appt_id}/accept', data={}, follow_redirects=True)

        with app_module.app.app_context():
            appt = app_module.db.session.get(app_module.Appointment, appt_id)
            assert appt.status == 'accepted'
            assert appt.appointment_time == '09:00'

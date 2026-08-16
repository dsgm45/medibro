"""
Tests for get_todays_schedule() after its N+1 query fix (batch-loading
doses and dose logs instead of querying per-medicine and per-dose).
These confirm the optimization didn't change the actual behavior -
correct taken/not-taken status, correct exclusion of inactive medicines.
"""
from datetime import date, datetime, timedelta
import app as app_module


class TestTodaysScheduleCorrectness:
    def test_untaken_dose_shows_as_not_taken(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Metformin', dosage='500mg', frequency='daily')
            app_module.db.session.add(med)
            app_module.db.session.commit()
            dose = app_module.MedicineDose(medicine_id=med.id, time='08:00')
            app_module.db.session.add(dose)
            app_module.db.session.commit()

            schedule = app_module.get_todays_schedule(patient_id)
            assert len(schedule) == 1
            assert schedule[0]['taken'] is False

    def test_taken_dose_shows_as_taken(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Metformin', dosage='500mg', frequency='daily')
            app_module.db.session.add(med)
            app_module.db.session.commit()
            dose = app_module.MedicineDose(medicine_id=med.id, time='08:00')
            app_module.db.session.add(dose)
            app_module.db.session.commit()
            log = app_module.MedicineDoseLog(dose_id=dose.id, log_date=date.today(), taken_at=datetime.utcnow())
            app_module.db.session.add(log)
            app_module.db.session.commit()

            schedule = app_module.get_todays_schedule(patient_id)
            assert len(schedule) == 1
            assert schedule[0]['taken'] is True

    def test_multiple_medicines_multiple_doses_correctly_matched(self, client, make_user):
        # The core risk in a batch-query rewrite: mixing up which taken
        # status belongs to which dose. Two medicines, two doses each,
        # only one specific dose marked taken - everything else must
        # stay correctly matched, not accidentally shared or swapped.
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            med_a = app_module.Medicine(patient_id=patient_id, name='Med A', dosage='1', frequency='daily')
            med_b = app_module.Medicine(patient_id=patient_id, name='Med B', dosage='1', frequency='daily')
            app_module.db.session.add_all([med_a, med_b])
            app_module.db.session.commit()

            dose_a1 = app_module.MedicineDose(medicine_id=med_a.id, time='08:00')
            dose_a2 = app_module.MedicineDose(medicine_id=med_a.id, time='20:00')
            dose_b1 = app_module.MedicineDose(medicine_id=med_b.id, time='09:00')
            dose_b2 = app_module.MedicineDose(medicine_id=med_b.id, time='21:00')
            app_module.db.session.add_all([dose_a1, dose_a2, dose_b1, dose_b2])
            app_module.db.session.commit()

            # Only dose_b1 marked taken
            log = app_module.MedicineDoseLog(dose_id=dose_b1.id, log_date=date.today(), taken_at=datetime.utcnow())
            app_module.db.session.add(log)
            app_module.db.session.commit()

            schedule = app_module.get_todays_schedule(patient_id)
            assert len(schedule) == 4

            taken_map = {entry['dose'].id: entry['taken'] for entry in schedule}
            assert taken_map[dose_a1.id] is False
            assert taken_map[dose_a2.id] is False
            assert taken_map[dose_b1.id] is True
            assert taken_map[dose_b2.id] is False

    def test_inactive_medicine_excluded(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            # Ended yesterday - should not appear in today's schedule
            med = app_module.Medicine(
                patient_id=patient_id, name='Old Med', dosage='1', frequency='daily',
                end_date=date.today() - timedelta(days=1)
            )
            app_module.db.session.add(med)
            app_module.db.session.commit()
            dose = app_module.MedicineDose(medicine_id=med.id, time='08:00')
            app_module.db.session.add(dose)
            app_module.db.session.commit()

            schedule = app_module.get_todays_schedule(patient_id)
            assert len(schedule) == 0

    def test_dose_taken_on_a_different_day_does_not_count_today(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Metformin', dosage='500mg', frequency='daily')
            app_module.db.session.add(med)
            app_module.db.session.commit()
            dose = app_module.MedicineDose(medicine_id=med.id, time='08:00')
            app_module.db.session.add(dose)
            app_module.db.session.commit()
            # Taken yesterday, not today
            log = app_module.MedicineDoseLog(
                dose_id=dose.id, log_date=date.today() - timedelta(days=1),
                taken_at=datetime.utcnow()
            )
            app_module.db.session.add(log)
            app_module.db.session.commit()

            schedule = app_module.get_todays_schedule(patient_id)
            assert len(schedule) == 1
            assert schedule[0]['taken'] is False


class TestMedicinesRouteStillWorks:
    def test_medicines_page_loads_with_active_medicine(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Metformin', dosage='500mg', frequency='daily')
            app_module.db.session.add(med)
            app_module.db.session.commit()
            dose = app_module.MedicineDose(medicine_id=med.id, time='08:00')
            app_module.db.session.add(dose)
            app_module.db.session.commit()

        from conftest import login
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/medicines')
        assert resp.status_code == 200
        assert b'Metformin' in resp.data
        assert b'08:00' in resp.data

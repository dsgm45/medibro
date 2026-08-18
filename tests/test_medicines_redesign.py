"""
Tests for the Medicines redesign: colored left-border cards, a single
consistent color for all time-of-day schedule items (icons, not color,
distinguish Morning/Afternoon/Evening since they don't differ in
meaning/urgency), and theme-aware chip-selector JS reading colors via
getComputedStyle instead of hardcoded hex.
"""
from datetime import date
import app as app_module
from conftest import login


class TestMedicinesUsesThemeVariables:
    def test_no_hardcoded_light_mode_colors(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/medicines')
        text = resp.data.decode()
        assert '#0f172a' not in text
        assert '#2563eb' not in text
        assert '#eff6ff' not in text
        assert 'var(--primary)' in text

    def test_chip_js_reads_colors_via_computed_style(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/medicines')
        text = resp.data.decode()
        assert 'getComputedStyle' in text


class TestMedicinesScheduleStillWorks:
    def test_schedule_shows_medicine_and_dose_time(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Metformin', dosage='500mg', start_date=date.today())
            app_module.db.session.add(med)
            app_module.db.session.commit()
            dose = app_module.MedicineDose(medicine_id=med.id, time='08:00')
            app_module.db.session.add(dose)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/medicines')
        assert b'Metformin' in resp.data
        assert b'08:00' in resp.data
        assert b'ti-sunrise' in resp.data

    def test_dose_toggle_form_present(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            med = app_module.Medicine(patient_id=patient_id, name='Metformin', start_date=date.today())
            app_module.db.session.add(med)
            app_module.db.session.commit()
            dose = app_module.MedicineDose(medicine_id=med.id, time='08:00')
            app_module.db.session.add(dose)
            app_module.db.session.commit()
            dose_id = dose.id

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/medicines')
        assert f'/medicines/dose/{dose_id}/toggle-taken'.encode() in resp.data


class TestMedicinesEmptyState:
    def test_no_medicines_shows_empty_messages(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/medicines')
        assert b'No scheduled doses for today' in resp.data
        assert b'No medicines added yet' in resp.data

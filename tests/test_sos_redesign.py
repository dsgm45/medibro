"""
Tests for the SOS redesign. This is the single most safety-critical
page in the app - a restyle must never reduce the visual urgency of
the emergency trigger or hide it in any state. Also introduces a
theme-aware --danger-text variable (the old hardcoded #7f1d1d would
have had poor contrast against the dark-mode danger-light background).
"""
from datetime import datetime
import app as app_module
from conftest import login


class TestSOSTriggerAlwaysPresent:
    def test_button_present_with_no_contacts(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/sos')
        assert b'Trigger SOS Alert' in resp.data

    def test_button_present_with_contacts(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            contact = app_module.EmergencyContact(patient_id=patient_id, contact_name='Priya', contact_phone='555-1234', relation='Spouse')
            app_module.db.session.add(contact)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/sos')
        assert b'Trigger SOS Alert' in resp.data

    def test_button_present_at_contact_limit(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            for i in range(5):
                c = app_module.EmergencyContact(patient_id=patient_id, contact_name=f'Contact{i}', contact_phone='555')
                app_module.db.session.add(c)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/sos')
        assert b'Trigger SOS Alert' in resp.data
        assert b'reached the limit of 5' in resp.data


class TestSOSVisualUrgencyPreserved:
    def test_trigger_button_uses_danger_color_not_muted(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/sos')
        text = resp.data.decode()
        idx = text.find('Trigger SOS Alert')
        preceding = text[max(0, idx - 300):idx]
        assert 'var(--danger)' in preceding
        assert 'font-weight: 700' in preceding

    def test_theme_aware_danger_text_used_not_hardcoded(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/sos')
        text = resp.data.decode()
        assert 'var(--danger-text)' in text
        # #7f1d1d correctly appears once, as --danger-text's own light-mode
        # definition in base.html's <style> block - the actual bug this
        # guards against is INLINE usage of the hardcoded hex, not the
        # variable's own definition existing on the page.
        assert 'color: #7f1d1d' not in text


class TestSOSContactManagementStillWorks:
    def test_add_contact_form_present(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/sos')
        assert b'name="contact_name"' in resp.data
        assert b'name="contact_phone"' in resp.data

    def test_delete_contact_form_present(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            contact = app_module.EmergencyContact(patient_id=patient_id, contact_name='Priya', contact_phone='555-1234')
            app_module.db.session.add(contact)
            app_module.db.session.commit()
            contact_id = contact.id

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/sos')
        assert f'/sos/contact/{contact_id}/delete'.encode() in resp.data

    def test_tel_link_uses_correct_phone(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            contact = app_module.EmergencyContact(patient_id=patient_id, contact_name='Priya', contact_phone='5551234567')
            app_module.db.session.add(contact)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/sos')
        assert b'tel:5551234567' in resp.data


class TestSOSHistory:
    def test_no_events_shows_empty_message(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/sos')
        assert b'No SOS alerts triggered yet' in resp.data

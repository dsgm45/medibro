"""Tests for the legal pages (Terms, Privacy, Disclaimer) and their footer links."""
from conftest import login


class TestLegalPagesAccessibleWithoutLogin:
    def test_terms_loads_without_login(self, client):
        resp = client.get('/legal/terms')
        assert resp.status_code == 200
        assert b'Terms of Service' in resp.data

    def test_privacy_loads_without_login(self, client):
        resp = client.get('/legal/privacy')
        assert resp.status_code == 200
        assert b'Privacy Policy' in resp.data

    def test_disclaimer_loads_without_login(self, client):
        resp = client.get('/legal/disclaimer')
        assert resp.status_code == 200
        assert b'Medical Disclaimer' in resp.data


class TestGoBackLink:
    def test_terms_has_go_back_link(self, client):
        resp = client.get('/legal/terms')
        assert b'Go Back' in resp.data
        assert b'history.back()' in resp.data

    def test_privacy_has_go_back_link(self, client):
        resp = client.get('/legal/privacy')
        assert b'Go Back' in resp.data

    def test_disclaimer_has_go_back_link(self, client):
        resp = client.get('/legal/disclaimer')
        assert b'Go Back' in resp.data


class TestLegalPageContent:
    def test_terms_covers_no_default_doctor_patient_relationship(self, client):
        resp = client.get('/legal/terms')
        assert b'doctor-patient relationship' in resp.data

    def test_terms_covers_prototype_stage_disclosure(self, client):
        resp = client.get('/legal/terms')
        assert b'not attorney-reviewed' in resp.data or b'not yet been reviewed by a lawyer' in resp.data

    def test_privacy_discloses_gemini_third_party_processing(self, client):
        resp = client.get('/legal/privacy')
        assert b'Gemini' in resp.data

    def test_privacy_does_not_overclaim_hipaa_compliance(self, client):
        resp = client.get('/legal/privacy')
        assert b'not currently a HIPAA-covered entity' in resp.data

    def test_disclaimer_has_visible_emergency_warning(self, client):
        resp = client.get('/legal/disclaimer')
        assert b'emergency' in resp.data.lower()

    def test_disclaimer_states_ai_output_is_not_a_diagnosis(self, client):
        resp = client.get('/legal/disclaimer')
        assert b'not a diagnosis' in resp.data.lower() or b'not be treated as a diagnosis' in resp.data.lower()

    def test_disclaimer_covers_emergency_detection_limitations(self, client):
        resp = client.get('/legal/disclaimer')
        assert b'not a guarantee' in resp.data.lower()

    def test_disclaimer_covers_sos_limitations(self, client):
        resp = client.get('/legal/disclaimer')
        assert b'SOS' in resp.data


class TestFooterLinks:
    def test_footer_present_when_logged_out(self, client):
        resp = client.get('/login')
        assert b'Terms of Service' in resp.data
        assert b'Privacy Policy' in resp.data
        assert b'Medical Disclaimer' in resp.data

    def test_footer_present_when_logged_in(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.get('/my-health')
        assert b'Terms of Service' in resp.data
        assert b'Privacy Policy' in resp.data
        assert b'Medical Disclaimer' in resp.data

    def test_landing_page_no_longer_overclaims_hipaa(self, client):
        resp = client.get('/')
        assert b'Privacy &amp; HIPAA' not in resp.data
        assert b'Privacy & HIPAA' not in resp.data

    def test_landing_page_has_no_placeholder_phone_number(self, client):
        resp = client.get('/')
        assert b'555-0199' not in resp.data

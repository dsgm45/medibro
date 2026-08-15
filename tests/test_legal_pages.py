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

    def test_landing_page_no_dead_footer_links(self, client):
        resp = client.get('/')
        assert b'About Us' not in resp.data
        assert b'Medical Board' not in resp.data
        assert b'Careers' not in resp.data

    def test_landing_page_shows_built_features_as_available(self, client):
        # AI Health Chat and Medicine Reminders are fully built - the
        # landing page must not undersell them as still upcoming.
        resp = client.get('/')
        text = resp.data.decode()
        ai_chat_section = text[max(0, text.index('AI Health Chat') - 500):text.index('AI Health Chat') + 200]
        assert 'Coming Soon' not in ai_chat_section

        med_section = text[max(0, text.index('Smart Medicine Reminders') - 500):text.index('Smart Medicine Reminders') + 200]
        assert 'Coming Soon' not in med_section

    def test_landing_page_shows_video_consultation_as_still_upcoming(self, client):
        # This one genuinely isn't built yet - the page must not overclaim it either.
        resp = client.get('/')
        text = resp.data.decode()
        video_section = text[max(0, text.index('Video Consultation') - 500):text.index('Video Consultation') + 200]
        assert 'Coming Soon' in video_section

    def test_landing_page_has_meta_description_and_favicon(self, client):
        resp = client.get('/')
        assert b'name="description"' in resp.data
        assert b'rel="icon"' in resp.data

    def test_landing_page_has_semantic_main_and_skip_link(self, client):
        resp = client.get('/')
        assert b'<main>' in resp.data
        assert b'Skip to main content' in resp.data

    def test_landing_page_has_mobile_nav_menu(self, client):
        resp = client.get('/')
        assert b'mobileNav' in resp.data
        assert b'Toggle navigation menu' in resp.data

    def test_landing_page_how_it_works_does_not_overclaim(self, client):
        resp = client.get('/')
        text = resp.data.decode()
        assert 'highly rated' not in text
        assert 'automatic digital prescriptions' not in text
        step3_section = text[max(0, text.index('Consult & Care') - 50):text.index('Consult & Care') + 300]
        assert 'video call' not in step3_section.lower()

    def test_landing_page_mobile_header_not_cluttered(self, client):
        # Log In / Sign Up are directly visible on mobile (not hidden in
        # the dropdown) but sized compactly so they fit alongside the
        # brand and hamburger icon without cramping - this replaced the
        # earlier "hide them entirely on mobile" design.
        resp = client.get('/')
        text = resp.data.decode()
        header_section = text[:text.index('id="mobileNav"')]
        assert 'px-3 py-1.5 text-xs' in header_section
        assert 'hidden md:inline-block' not in header_section

    def test_landing_page_hero_shows_product_preview(self, client):
        resp = client.get('/')
        assert b'Blood Pressure' in resp.data
        assert b'Next Appointment' in resp.data

    def test_landing_page_trust_signals_present(self, client):
        resp = client.get('/')
        assert b'Doctor accounts manually verified' in resp.data
        assert b'You control your data' in resp.data

    def test_landing_page_no_demo_dashboard_link(self, client):
        resp = client.get('/')
        assert b'Demo Dashboard' not in resp.data

    def test_landing_page_trust_signals_use_grid(self, client):
        resp = client.get('/')
        assert b'grid grid-cols-1 sm:grid-cols-2 gap-x-6' in resp.data

    def test_landing_page_has_hero_carousel_with_five_slides(self, client):
        resp = client.get('/')
        assert resp.data.count(b'class="carousel-slide') == 5
        assert resp.data.count(b'carousel-dot') >= 5
        for title in [b'My Health', b'AI Health Chat', b'Medicine Reminders', b'Book Appointment', b'Vitals Tracking']:
            assert title in resp.data

    def test_landing_page_has_ai_safety_section(self, client):
        resp = client.get('/')
        assert b'How MediBro Keeps AI Safe' in resp.data

    def test_landing_page_uses_compiled_css_not_cdn(self, client):
        resp = client.get('/')
        assert b'cdn.tailwindcss.com' not in resp.data
        assert b'static/css/tailwind.css' in resp.data

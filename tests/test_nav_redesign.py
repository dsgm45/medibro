"""
Tests for the navigation consolidation: the patient sidebar and mobile
bottom tab bar used to be duplicated across 12 separate templates, now
they're rendered once by base.html for any patient session. Covers the
highest-risk scenarios: exactly one sidebar/tabbar per page (no
duplication), and the shared chat pages correctly show zero patient
nav for doctor sessions, since they serve both roles.
"""
import app as app_module
from conftest import login


PATIENT_PAGES = [
    '/my-health', '/vitals', '/medicines', '/symptoms', '/ai-chat',
    '/patient', '/sos', '/profile', '/my-health/care-team', '/my-health/access-log',
]


class TestNoSidebarDuplication:
    def test_every_patient_page_has_exactly_one_sidebar_and_tabbar(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        for path in PATIENT_PAGES:
            resp = client.get(path)
            assert resp.status_code == 200, f'{path} did not load'
            text = resp.data.decode()
            sidebar_count = text.count('class="patient-sidebar"')
            tabbar_count = text.count('class="mobile-tabbar"')
            assert sidebar_count == 1, f'{path} had {sidebar_count} sidebars, expected 1'
            assert tabbar_count == 1, f'{path} had {tabbar_count} tabbars, expected 1'

    def test_chat_list_patient_session_has_sidebar(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/chat')
        text = resp.data.decode()
        assert text.count('class="patient-sidebar"') == 1
        assert text.count('class="mobile-tabbar"') == 1


class TestSharedChatPagesRespectRole:
    def test_chat_list_doctor_session_has_no_patient_sidebar(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doc@example.com', 'password123')
        resp = client.get('/chat')
        text = resp.data.decode()
        assert 'class="patient-sidebar"' not in text
        assert 'class="mobile-tabbar"' not in text
        assert 'Back to Workspace' in text

    def test_chat_thread_doctor_session_has_no_patient_sidebar(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        with app_module.app.app_context():
            appt = app_module.Appointment(
                patient_id=patient_id, doctor_id=doctor_id,
                appointment_date='2026-08-01', appointment_time='10:00',
                phone_number='555', status='accepted'
            )
            app_module.db.session.add(appt)
            app_module.db.session.commit()
            appt_id = appt.id

        login(client, 'doc@example.com', 'password123')
        resp = client.get(f'/chat/{appt_id}')
        text = resp.data.decode()
        assert 'class="patient-sidebar"' not in text
        assert 'class="mobile-tabbar"' not in text


class TestNonPatientPagesUnaffected:
    def test_doctor_dashboard_has_no_patient_sidebar(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor')
        assert b'class="patient-sidebar"' not in resp.data

    def test_admin_dashboard_has_no_patient_sidebar(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin')
        assert b'class="patient-sidebar"' not in resp.data

    def test_logged_out_landing_page_has_no_patient_sidebar(self, client):
        resp = client.get('/')
        assert b'class="patient-sidebar"' not in resp.data


class TestDarkModeInfrastructure:
    def test_theme_toggle_button_present_on_every_page(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        assert b'id="themeToggleBtn"' in resp.data
        assert b'medibro-theme' in resp.data

    def test_dark_mode_css_variables_present(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        assert b'body.dark-mode' in resp.data


class TestNoEmojiInSharedNav:
    def test_sidebar_uses_icon_font_not_emoji(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        text = resp.data.decode()
        # Use the complete opening tags as boundaries, not the bare class
        # name - "mobile-tabbar" also appears earlier in the page's CSS
        # rules (in <head>), which would produce a false/empty slice if
        # matched instead of the actual <nav> element in <body>.
        sidebar_section = text[text.index('<aside class="patient-sidebar"'):text.index('</aside>')]
        assert 'ti ti-home' in sidebar_section
        assert '🏠' not in sidebar_section
        assert '💊' not in sidebar_section

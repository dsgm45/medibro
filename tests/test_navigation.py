"""Tests for the role-aware logo link - goes to portal home when logged
in, landing page when logged out. This also fixes the "no way back" gap
on pages like Notifications, since every page shares this same nav."""
import app as app_module
from conftest import login


class TestPortalHomeLink:
    def test_logged_out_logo_goes_to_landing_page(self, client):
        resp = client.get('/login')
        assert b'href="/" class="nav-brand"' in resp.data

    def test_patient_logo_goes_to_my_health(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.get('/my-health')
        assert b'href="/my-health" class="nav-brand"' in resp.data

    def test_doctor_logo_goes_to_doctor_dashboard(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doc@example.com', 'password123')

        resp = client.get('/doctor')
        assert b'href="/doctor" class="nav-brand"' in resp.data

    def test_admin_logo_goes_to_admin_dashboard(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        resp = client.get('/admin')
        assert b'href="/admin" class="nav-brand"' in resp.data

    def test_notifications_page_logo_still_routes_home(self, client, make_user):
        # This is the specific gap that was reported: no way back from
        # the Notifications page. The shared nav now fixes this generally.
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.get('/notifications')
        assert b'href="/my-health" class="nav-brand"' in resp.data

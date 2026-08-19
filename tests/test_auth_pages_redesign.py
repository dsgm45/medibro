"""
Tests for the Login and Register page redesign. These already used CSS
variables for color, but hadn't been brought up to the same visual
language as the rest of the app (Fraunces headings, themed input
backgrounds, matching border-radius). Also fixes Register's password
checklist JS, which hardcoded #15803d/#64748b instead of reading the
theme.
"""
from conftest import login


class TestLoginRedesign:
    def test_login_page_loads(self, client):
        resp = client.get('/login')
        assert resp.status_code == 200
        assert b'Welcome back' in resp.data
        assert b'name="email"' in resp.data
        assert b'name="password"' in resp.data

    def test_login_no_hardcoded_colors(self, client):
        resp = client.get('/login')
        text = resp.data.decode()
        assert '#2563eb' not in text
        assert '#0f172a' not in text


class TestRegisterRedesign:
    def test_register_page_loads(self, client):
        resp = client.get('/register')
        assert resp.status_code == 200
        assert b'Create your account' in resp.data
        assert b'name="full_name"' in resp.data
        assert b'name="role"' in resp.data

    def test_password_checklist_reads_theme_not_hardcoded(self, client):
        resp = client.get('/register')
        text = resp.data.decode()
        assert 'getComputedStyle' in text
        assert '#15803d' not in text
        assert 'color: #64748b' not in text

    def test_specialty_field_toggle_present(self, client):
        resp = client.get('/register')
        assert b'toggleSpecialty' in resp.data
        assert b'specialtyField' in resp.data

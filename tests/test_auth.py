"""Tests for registration, login, and login rate limiting."""
import app as app_module
from conftest import login


class TestRegistration:
    def test_patient_registration_succeeds_and_is_auto_approved(self, client):
        resp = client.post('/register', data={
            'email': 'newpatient@example.com',
            'password': 'password123',
            'full_name': 'New Patient',
            'role': 'patient',
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app_module.app.app_context():
            user = app_module.User.query.filter_by(email='newpatient@example.com').first()
            assert user is not None
            assert user.status == 'approved'
            assert user.role == 'patient'

    def test_doctor_registration_is_pending_by_default(self, client):
        client.post('/register', data={
            'email': 'newdoctor@example.com',
            'password': 'password123',
            'full_name': 'New Doctor',
            'role': 'doctor',
            'specialty': 'Cardiology',
        }, follow_redirects=True)

        with app_module.app.app_context():
            user = app_module.User.query.filter_by(email='newdoctor@example.com').first()
            assert user is not None
            assert user.status == 'pending'

    def test_registration_rejects_invalid_email(self, client):
        client.post('/register', data={
            'email': 'not-an-email',
            'password': 'password123',
            'full_name': 'Someone',
            'role': 'patient',
        })
        with app_module.app.app_context():
            assert app_module.User.query.filter_by(email='not-an-email').first() is None

    def test_registration_rejects_weak_password(self, client):
        # No digits - should fail the letters+numbers requirement
        client.post('/register', data={
            'email': 'weakpass@example.com',
            'password': 'onlyletters',
            'full_name': 'Someone',
            'role': 'patient',
        })
        with app_module.app.app_context():
            assert app_module.User.query.filter_by(email='weakpass@example.com').first() is None

    def test_registration_rejects_short_password(self, client):
        client.post('/register', data={
            'email': 'shortpass@example.com',
            'password': 'a1',
            'full_name': 'Someone',
            'role': 'patient',
        })
        with app_module.app.app_context():
            assert app_module.User.query.filter_by(email='shortpass@example.com').first() is None

    def test_registration_rejects_duplicate_email(self, client, make_user):
        make_user('existing@example.com', 'password123')
        client.post('/register', data={
            'email': 'existing@example.com',
            'password': 'password123',
            'full_name': 'Duplicate',
            'role': 'patient',
        })
        with app_module.app.app_context():
            count = app_module.User.query.filter_by(email='existing@example.com').count()
            assert count == 1


class TestLogin:
    def test_login_succeeds_with_correct_credentials(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        resp = login(client, 'patient@example.com', 'password123')
        assert resp.status_code == 302
        assert '/my-health' in resp.location

    def test_login_fails_with_wrong_password(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        resp = login(client, 'patient@example.com', 'wrongpassword')
        assert resp.status_code == 200  # re-renders login page, no redirect
        with client.session_transaction() as sess:
            assert 'user_id' not in sess

    def test_login_fails_for_nonexistent_email(self, client):
        resp = login(client, 'nobody@example.com', 'whatever123')
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert 'user_id' not in sess

    def test_pending_doctor_cannot_login(self, client, make_user):
        make_user('pendingdoc@example.com', 'password123', role='doctor', status='pending')
        resp = login(client, 'pendingdoc@example.com', 'password123')
        with client.session_transaction() as sess:
            assert 'user_id' not in sess

    def test_suspended_user_cannot_login(self, client, make_user):
        make_user('suspended@example.com', 'password123', role='patient', status='suspended')
        resp = login(client, 'suspended@example.com', 'password123')
        with client.session_transaction() as sess:
            assert 'user_id' not in sess

    def test_login_redirects_by_role(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        resp = login(client, 'doc@example.com', 'password123')
        assert '/doctor' in resp.location

        make_user('admin2@example.com', 'password123', role='hospital', status='approved')
        resp = login(client, 'admin2@example.com', 'password123')
        assert '/admin' in resp.location


class TestLoginRateLimiting:
    def test_repeated_failed_logins_get_rate_limited(self, client, make_user):
        make_user('ratelimited@example.com', 'correctpassword1', role='patient')

        # Exhaust the attempt limit with wrong passwords
        for _ in range(app_module.MAX_LOGIN_ATTEMPTS):
            login(client, 'ratelimited@example.com', 'wrongpassword')

        # Even the CORRECT password should now be rejected due to rate limiting
        resp = login(client, 'ratelimited@example.com', 'correctpassword1')
        with client.session_transaction() as sess:
            assert 'user_id' not in sess

    def test_rate_limit_does_not_affect_other_accounts(self, client, make_user):
        make_user('victim@example.com', 'password123', role='patient')
        make_user('other@example.com', 'password123', role='patient')

        for _ in range(app_module.MAX_LOGIN_ATTEMPTS):
            login(client, 'victim@example.com', 'wrongpassword')

        # A completely different account should be unaffected
        resp = login(client, 'other@example.com', 'password123')
        with client.session_transaction() as sess:
            assert sess.get('user_id') is not None

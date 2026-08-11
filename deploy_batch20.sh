#!/bin/bash
set -e

echo "=== MediBro: Automated test suite ==="

if [ ! -f "app.py" ]; then
  echo "ERROR: app.py not found. cd into your medimind project folder first, then re-run this script."
  exit 1
fi

mkdir -p tests

cat > requirements-dev.txt << 'FILEEOF_1'
pytest==8.3.3
FILEEOF_1
cat > pytest.ini << 'FILEEOF_2'
[pytest]
testpaths = tests
FILEEOF_2
cat > tests/conftest.py << 'FILEEOF_3'
"""
Shared pytest fixtures for the MediBro test suite.

IMPORTANT ARCHITECTURAL NOTE: app.py does not use an application factory
pattern - it creates the Flask app, database connection, and runs
init_db()/run_migrations() as side effects at import time, reading
DATABASE_URL from the environment as it does. That means environment
variables must be set BEFORE app.py is imported, which is why this file
sets them at module level before the `import app` statement below, rather
than inside a fixture (fixtures run too late - after collection/import).

Each test gets a clean database via the autouse `reset_database` fixture,
which drops and recreates all tables before every test function. This
bypasses Alembic entirely (drop_all/create_all, not migrations) which is
fine for a throwaway test database - we only care about matching the
current models, not migration history.
"""
import os
import sys
import tempfile
from pathlib import Path
import pytest

# tests/ is a subdirectory of the project root, where app.py lives. Depending
# on pytest's import mode, the project root isn't guaranteed to be on
# sys.path automatically just because conftest.py is here - so this is made
# explicit rather than relying on pytest's internal behavior.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- Set environment BEFORE importing app ---
_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.environ['DATABASE_URL'] = f'sqlite:///{_db_path}'
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production-use'
os.environ['ADMIN_INITIAL_PASSWORD'] = 'TestAdminPass1'

import app as app_module  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


@pytest.fixture(scope='session', autouse=True)
def _configure_app():
    app_module.app.config['TESTING'] = True
    app_module.app.config['WTF_CSRF_ENABLED'] = False
    yield
    try:
        os.close(_db_fd)
        os.unlink(_db_path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def reset_database():
    """Fresh schema before every test. Order matters for FK constraints,
    which is why drop_all/create_all (which handles dependency order
    internally) is used rather than manually deleting rows table by table."""
    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()
    yield


@pytest.fixture(autouse=True)
def reset_rate_limits():
    """LOGIN_ATTEMPTS and REGISTER_ATTEMPTS are module-level in-memory
    dicts that persist for the life of the process - they are NOT reset by
    reset_database, since that only touches the database. Without this,
    tests that make several requests from the same fake test-client
    IP/email (which is most of them) would start failing for the wrong
    reason once enough tests have run to trip the rate limiter."""
    app_module.LOGIN_ATTEMPTS.clear()
    app_module.REGISTER_ATTEMPTS.clear()
    yield


@pytest.fixture
def client():
    with app_module.app.test_client() as test_client:
        yield test_client


@pytest.fixture
def make_user():
    """Factory fixture: create a user directly via the ORM, bypassing the
    registration route/validation, for tests that need a user to already
    exist rather than testing registration itself."""
    def _make_user(email, password, role='patient', status='approved', full_name='Test User', **kwargs):
        with app_module.app.app_context():
            user = app_module.User(
                email=email,
                password_hash=generate_password_hash(password),
                role=role,
                full_name=full_name,
                status=status,
                **kwargs
            )
            app_module.db.session.add(user)
            app_module.db.session.commit()
            return user.id
    return _make_user


def login(client, email, password):
    return client.post('/login', data={'email': email, 'password': password}, follow_redirects=False)
FILEEOF_3
cat > tests/test_auth.py << 'FILEEOF_4'
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
FILEEOF_4
cat > tests/test_access_control.py << 'FILEEOF_5'
"""Tests for role-based access control - the core security boundary of the app."""
from conftest import login


class TestUnauthenticatedAccess:
    def test_my_health_requires_login(self, client):
        resp = client.get('/my-health', follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.location

    def test_doctor_dashboard_requires_login(self, client):
        resp = client.get('/doctor', follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.location

    def test_admin_dashboard_requires_login(self, client):
        resp = client.get('/admin', follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.location


class TestCrossRoleAccess:
    def test_patient_cannot_access_doctor_dashboard(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.get('/doctor', follow_redirects=False)
        assert resp.status_code == 302
        assert '/doctor' not in resp.location or resp.location == '/'

    def test_patient_cannot_access_admin_dashboard(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.get('/admin', follow_redirects=False)
        assert resp.status_code == 302

    def test_doctor_cannot_access_admin_dashboard(self, client, make_user):
        make_user('doctor@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doctor@example.com', 'password123')

        resp = client.get('/admin', follow_redirects=False)
        assert resp.status_code == 302

    def test_doctor_cannot_access_patient_dashboard(self, client, make_user):
        make_user('doctor@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doctor@example.com', 'password123')

        resp = client.get('/patient', follow_redirects=False)
        assert resp.status_code == 302

    def test_admin_cannot_access_patient_dashboard(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        resp = client.get('/patient', follow_redirects=False)
        assert resp.status_code == 302


class TestSameRoleAccess:
    def test_patient_can_access_own_dashboard(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.get('/my-health')
        assert resp.status_code == 200

    def test_doctor_can_access_own_dashboard(self, client, make_user):
        make_user('doctor@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doctor@example.com', 'password123')

        resp = client.get('/doctor')
        assert resp.status_code == 200

    def test_admin_can_access_own_dashboard(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        resp = client.get('/admin')
        assert resp.status_code == 200


class TestOwnershipChecks:
    """A logged-in user of the CORRECT role trying to access another
    user's specific data - a different, equally important boundary from
    role checks."""

    def test_patient_cannot_cancel_another_patients_appointment(self, client, make_user):
        import app as app_module

        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        patient_a_id = make_user('patienta@example.com', 'password123', role='patient')
        make_user('patientb@example.com', 'password123', role='patient')

        with app_module.app.app_context():
            appt = app_module.Appointment(
                patient_id=patient_a_id, doctor_id=doctor_id,
                appointment_date='2026-12-01', appointment_time='10:00',
                phone_number='555-0000', status='pending'
            )
            app_module.db.session.add(appt)
            app_module.db.session.commit()
            appt_id = appt.id

        # Log in as patient B and try to cancel patient A's appointment
        login(client, 'patientb@example.com', 'password123')
        client.post(f'/my-appointment/{appt_id}/cancel', follow_redirects=True)

        with app_module.app.app_context():
            appt = app_module.Appointment.query.get(appt_id)
            assert appt.status == 'pending'  # unchanged - patient B was not authorized
FILEEOF_5
cat > tests/test_booking.py << 'FILEEOF_6'
"""Tests for appointment booking, including the double-booking prevention logic."""
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


class TestBooking:
    def test_patient_can_book_appointment(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved', specialty='Cardiology')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        _book(client, doctor_id)

        with app_module.app.app_context():
            appts = app_module.Appointment.query.all()
            assert len(appts) == 1
            assert appts[0].status == 'pending'
            assert appts[0].doctor_id == doctor_id

    def test_booking_requires_phone_number(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        client.post('/book-appointment', data={
            'doctor_id': str(doctor_id),
            'appointment_date': '2026-12-01',
            'appointment_time': '10:00',
            'phone_number': '',
            'reason': 'Checkup',
        }, follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.Appointment.query.count() == 0

    def test_booking_requires_date_and_time(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        client.post('/book-appointment', data={
            'doctor_id': str(doctor_id),
            'appointment_date': '',
            'appointment_time': '',
            'phone_number': '555-1234',
        }, follow_redirects=True)

        with app_module.app.app_context():
            assert app_module.Appointment.query.count() == 0

    def test_booking_rejects_malformed_doctor_id(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.post('/book-appointment', data={
            'doctor_id': 'not-a-number',
            'appointment_date': '2026-12-01',
            'appointment_time': '10:00',
            'phone_number': '555-1234',
        }, follow_redirects=True)

        # Should be handled gracefully (redirect with flash), not a 500 crash
        assert resp.status_code == 200
        with app_module.app.app_context():
            assert app_module.Appointment.query.count() == 0


class TestDoubleBookingPrevention:
    def test_same_doctor_same_slot_is_rejected(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient_a@example.com', 'password123', role='patient')
        make_user('patient_b@example.com', 'password123', role='patient')

        login(client, 'patient_a@example.com', 'password123')
        _book(client, doctor_id, date='2026-12-05', time='14:00')

        client.post('/logout', follow_redirects=True)
        login(client, 'patient_b@example.com', 'password123')
        _book(client, doctor_id, date='2026-12-05', time='14:00')

        with app_module.app.app_context():
            appts = app_module.Appointment.query.filter_by(
                doctor_id=doctor_id, appointment_date='2026-12-05', appointment_time='14:00'
            ).all()
            assert len(appts) == 1  # second booking attempt was rejected

    def test_different_time_same_doctor_is_allowed(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        _book(client, doctor_id, date='2026-12-05', time='09:00')
        _book(client, doctor_id, date='2026-12-05', time='10:00')

        with app_module.app.app_context():
            assert app_module.Appointment.query.filter_by(doctor_id=doctor_id).count() == 2

    def test_different_doctor_same_slot_is_allowed(self, client, make_user):
        doctor_a_id = make_user('doca@example.com', 'password123', role='doctor', status='approved')
        doctor_b_id = make_user('docb@example.com', 'password123', role='doctor', status='approved')
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        _book(client, doctor_a_id, date='2026-12-05', time='10:00')
        _book(client, doctor_b_id, date='2026-12-05', time='10:00')

        with app_module.app.app_context():
            assert app_module.Appointment.query.count() == 2

    def test_declined_appointment_frees_the_slot(self, client, make_user):
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        make_user('patient_a@example.com', 'password123', role='patient')
        make_user('patient_b@example.com', 'password123', role='patient')

        login(client, 'patient_a@example.com', 'password123')
        _book(client, doctor_id, date='2026-12-05', time='11:00')

        with app_module.app.app_context():
            appt = app_module.Appointment.query.filter_by(doctor_id=doctor_id).first()
            appt.status = 'declined'
            app_module.db.session.commit()

        client.post('/logout', follow_redirects=True)
        login(client, 'patient_b@example.com', 'password123')
        _book(client, doctor_id, date='2026-12-05', time='11:00')

        with app_module.app.app_context():
            active = app_module.Appointment.query.filter_by(
                doctor_id=doctor_id, appointment_date='2026-12-05', appointment_time='11:00'
            ).filter(app_module.Appointment.status.in_(['pending', 'accepted'])).all()
            assert len(active) == 1  # patient B's new booking went through
FILEEOF_6
cat > tests/README.md << 'FILEEOF_7'
# MediBro Test Suite

## What this covers

- **Registration**: valid signup, doctor accounts auto-pending, invalid email
  rejected, weak/short passwords rejected, duplicate emails rejected
- **Login**: correct/incorrect credentials, pending doctors blocked,
  suspended accounts blocked, role-based redirect after login
- **Rate limiting**: repeated failed logins get locked out (even with the
  correct password once locked), and the lockout doesn't leak across
  different accounts
- **Access control**: every role boundary (patient/doctor/admin can't reach
  each other's dashboards), and an ownership check (one patient can't cancel
  another patient's appointment)
- **Booking**: successful booking, required-field validation, and the
  double-booking prevention logic (same slot blocked, different time/doctor
  allowed, a declined appointment frees the slot back up)

This is a first pass covering the core flows, not exhaustive coverage of
every route in the app.

## Important: I could not run these tests myself

Every other batch tonight got verified against Render's actual build logs
or by executing the logic directly in this sandbox. This is different -
`pytest`, `Flask-SQLAlchemy`, and `Flask-WTF` aren't available in this
sandbox (no network access to install them), so **this test suite has only
been checked for Python syntax validity and manually cross-referenced
against the actual routes in `app.py` - it has never actually been run.**

Please run it and paste back the output, including any failures. That's
the only way to actually confirm this works, and I'd rather fix real
failures than have you discover a broken test suite later.

## How to run it

```
cd /Users/dpk/Downloads/medimind
pip install -r requirements-dev.txt --break-system-packages
pytest -v
```

If a test fails, the `-v` flag will show you exactly which one and why -
paste that output back and I'll fix it.

## Notes on how this is built

- `app.py` doesn't use an application-factory pattern - it creates the
  Flask app and runs database setup as side effects at import time. To test
  this safely, `conftest.py` points `DATABASE_URL` at a temporary SQLite
  file *before* importing `app.py`, so your real database is never touched
  by running these tests.
- Every test gets a completely fresh database (all tables dropped and
  recreated) before it runs, so tests can't leak state into each other via
  the database.
- Two in-memory rate-limiter dictionaries in `app.py` (`LOGIN_ATTEMPTS`,
  `REGISTER_ATTEMPTS`) live outside the database and don't get cleared by
  a fresh database - they're explicitly reset before every test too, or
  tests that make several login/registration attempts would start failing
  each other for unrelated reasons.
FILEEOF_7

echo "All files written."
echo ""
echo "=== Running the test suite now ==="
echo ""

if ! command -v pytest &> /dev/null; then
  echo "pytest not installed yet. Installing test dependencies..."
  pip install -r requirements-dev.txt --break-system-packages
fi

pytest -v

echo ""
echo "=== Test run complete - see results above ==="
echo ""
echo "IMPORTANT: I could not run this test suite myself before giving it to"
echo "you (no network access in my environment to install pytest/Flask-"
echo "SQLAlchemy/Flask-WTF). Please paste the output above back to me,"
echo "especially if anything failed, so I can fix it."
echo ""
read -p "If the tests look good (or you want to commit anyway to discuss failures), press Enter to commit and push, or Ctrl+C to stop here: "

git add requirements-dev.txt pytest.ini tests/
git commit -m "Add automated test suite covering auth, access control, and booking"
git push origin main

echo ""
echo "=== Done. ==="
echo "This does NOT affect your live Render app - requirements-dev.txt is"
echo "separate from requirements.txt, so Render's build won't try to"
echo "install pytest or run these tests. They're for local development only,"
echo "though having them in the repo is worth mentioning if anyone asks how"
echo "seriously this project is engineered."

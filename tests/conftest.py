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
import types as _pytypes
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
                password_hash=generate_password_hash(password, method='pbkdf2:sha256'),
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


class _FakeGenerateContentConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeThinkingConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakePart:
    @staticmethod
    def from_text(text=''):
        return {'text': text}


class _FakeContent:
    def __init__(self, role=None, parts=None):
        self.role = role
        self.parts = parts


@pytest.fixture(autouse=True)
def fake_google_genai_types(monkeypatch):
    """Injects a fake google.genai.types module so `from google.genai
    import types` succeeds inside any AI-calling code, without needing the
    real google-genai package installed. It's a heavy dependency (pulls in
    `cryptography`, which needs a Rust/OpenSSL toolchain to build from
    source on some systems) and none of these tests make a real API call
    anyway, so there's no good reason to require it just to run tests.
    Restored automatically after each test via monkeypatch."""
    fake_types_module = _pytypes.ModuleType('google.genai.types')
    fake_types_module.GenerateContentConfig = _FakeGenerateContentConfig
    fake_types_module.ThinkingConfig = _FakeThinkingConfig
    fake_types_module.Part = _FakePart
    fake_types_module.Content = _FakeContent

    fake_genai_module = _pytypes.ModuleType('google.genai')
    fake_genai_module.types = fake_types_module

    fake_google_module = _pytypes.ModuleType('google')
    fake_google_module.genai = fake_genai_module

    monkeypatch.setitem(sys.modules, 'google', fake_google_module)
    monkeypatch.setitem(sys.modules, 'google.genai', fake_genai_module)
    monkeypatch.setitem(sys.modules, 'google.genai.types', fake_types_module)
    yield

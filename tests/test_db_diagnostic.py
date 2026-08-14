"""
Tests for the Alembic diagnostic page and its fix mechanism.

The test database is built via db.create_all(), which always creates the
full CURRENT schema (matching the live models) regardless of migration
history - so in tests, the "true" detected state is always the latest
migration in the chain. Tests simulate a stale alembic_version value
against that always-current schema to verify the fix correctly detects
and corrects the drift.
"""
import app as app_module
from conftest import login


def _set_tracked_revision(revision):
    with app_module.app.app_context():
        app_module.db.session.execute(app_module.text(
            'CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)'
        ))
        app_module.db.session.execute(app_module.text('DELETE FROM alembic_version'))
        app_module.db.session.execute(app_module.text(
            "INSERT INTO alembic_version (version_num) VALUES (:rev)"
        ), {'rev': revision})
        app_module.db.session.commit()


def _get_tracked_revision():
    with app_module.app.app_context():
        return app_module.db.session.execute(app_module.text(
            'SELECT version_num FROM alembic_version'
        )).scalar()


class TestDbDiagnosticAccessControl:
    def test_requires_login(self, client):
        resp = client.get('/admin/db-diagnostic', follow_redirects=False)
        assert resp.status_code == 302

    def test_patient_cannot_access(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.get('/admin/db-diagnostic', follow_redirects=False)
        assert resp.status_code == 302

    def test_doctor_cannot_access(self, client, make_user):
        make_user('doctor@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doctor@example.com', 'password123')

        resp = client.get('/admin/db-diagnostic', follow_redirects=False)
        assert resp.status_code == 302

    def test_admin_can_access(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        resp = client.get('/admin/db-diagnostic')
        assert resp.status_code == 200
        assert b'Database Migration Diagnostic' in resp.data


class TestDbDiagnosticDetection:
    def test_detects_latest_revision_from_a_fresh_full_schema(self, client, make_user):
        # db.create_all() always builds the full current schema, so the
        # detected revision should always be the last one in the chain.
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        resp = client.get('/admin/db-diagnostic')
        assert app_module.MIGRATION_CHAIN[-1][0].encode() in resp.data

    def test_shows_migration_chain(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        resp = client.get('/admin/db-diagnostic')
        assert b'baseline_v1' in resp.data
        assert b'optional_time_v1' in resp.data

    def test_shows_existing_tables(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        resp = client.get('/admin/db-diagnostic')
        assert b'user' in resp.data
        assert b'appointment' in resp.data


class TestDbDiagnosticFix:
    def test_fix_corrects_stale_tracking_to_the_true_current_state(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        _set_tracked_revision('baseline_v1')  # stale - way behind the real (full) schema

        client.post('/admin/db-diagnostic/fix-tracking', follow_redirects=True)

        assert _get_tracked_revision() == app_module.MIGRATION_CHAIN[-1][0]

    def test_fix_makes_no_change_when_already_correct(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        latest = app_module.MIGRATION_CHAIN[-1][0]
        _set_tracked_revision(latest)

        client.post('/admin/db-diagnostic/fix-tracking', follow_redirects=True)

        assert _get_tracked_revision() == latest

    def test_fix_requires_admin(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.post('/admin/db-diagnostic/fix-tracking', follow_redirects=False)
        assert resp.status_code == 302

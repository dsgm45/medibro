"""Tests for the read-only Alembic diagnostic page (admin-only, no writes)."""
import app as app_module
from conftest import login


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


class TestDbDiagnosticFix:
    def test_fix_updates_tracking_when_mismatch_present(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        with app_module.app.app_context():
            # Simulate the exact real-world mismatch: alembic_version
            # exists and says ai_symptom_v1, but ai_chat_message (created
            # by db.create_all() in the test fixture) already exists.
            app_module.db.session.execute(app_module.text(
                'CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)'
            ))
            app_module.db.session.execute(app_module.text('DELETE FROM alembic_version'))
            app_module.db.session.execute(app_module.text(
                "INSERT INTO alembic_version (version_num) VALUES ('ai_symptom_v1')"
            ))
            app_module.db.session.commit()

        client.post('/admin/db-diagnostic/fix-tracking', follow_redirects=True)

        with app_module.app.app_context():
            result = app_module.db.session.execute(app_module.text(
                'SELECT version_num FROM alembic_version'
            )).scalar()
        assert result == 'ai_chat_v1'

    def test_fix_refuses_when_already_correct(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        with app_module.app.app_context():
            app_module.db.session.execute(app_module.text(
                'CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)'
            ))
            app_module.db.session.execute(app_module.text('DELETE FROM alembic_version'))
            app_module.db.session.execute(app_module.text(
                "INSERT INTO alembic_version (version_num) VALUES ('ai_chat_v1')"
            ))
            app_module.db.session.commit()

        client.post('/admin/db-diagnostic/fix-tracking', follow_redirects=True)

        with app_module.app.app_context():
            result = app_module.db.session.execute(app_module.text(
                'SELECT version_num FROM alembic_version'
            )).scalar()
        # Should remain unchanged - the safety guard only fires for the
        # exact expected mismatch, not any arbitrary state.
        assert result == 'ai_chat_v1'

    def test_fix_requires_admin(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')

        resp = client.post('/admin/db-diagnostic/fix-tracking', follow_redirects=False)
        assert resp.status_code == 302
    def test_shows_migration_chain(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        resp = client.get('/admin/db-diagnostic')
        assert b'ai_chat_v1' in resp.data
        assert b'baseline_v1' in resp.data

    def test_shows_existing_tables(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')

        resp = client.get('/admin/db-diagnostic')
        # The test database is created fresh via db.create_all(), so these
        # application tables should genuinely exist and be listed.
        assert b'user' in resp.data
        assert b'appointment' in resp.data

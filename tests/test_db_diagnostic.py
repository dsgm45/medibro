"""Tests for the read-only Alembic diagnostic page (admin-only, no writes)."""
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


class TestDbDiagnosticContent:
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

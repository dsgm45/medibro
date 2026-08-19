"""
Tests for the Admin/Hospital role redesign - a whole role that had
never been given the mockup treatment. Covers the main dashboard
(stats, SOS alerts, deletion requests, doctor/patient directories, the
theme-aware volume chart) and 3 smaller utility pages (audit log,
visit notes, DB diagnostic) that still carried old colors and one
remaining emoji.
"""
from datetime import datetime
import app as app_module
from conftest import login


class TestAdminDashboardUsesThemeVariables:
    def test_no_hardcoded_light_mode_colors(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin')
        text = resp.data.decode()
        assert '#fff7ed' not in text
        assert '#6d28d9' not in text
        assert '#0ea5e9' not in text
        assert '#eff6ff' not in text
        assert 'var(--primary)' in text

    def test_no_emoji_anywhere(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin')
        text = resp.data.decode()
        for emoji in ['⏳', '⬇', '🚨']:
            assert emoji not in text, f'{emoji} still found on Admin Dashboard'

    def test_volume_chart_reads_colors_via_computed_style(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin')
        assert b'getComputedStyle' in resp.data


class TestAdminDashboardFunctionsPreserved:
    def test_stats_cards_show_values(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin')
        assert b'Total Patients' in resp.data
        assert b'Active Doctors' in resp.data
        assert b'Pending Approvals' in resp.data

    def test_doctor_approve_reject_forms_present(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='pending')
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin')
        assert b'Approve' in resp.data
        assert b'Reject' in resp.data

    def test_patient_suspend_activate_shown(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin')
        assert b'Suspend' in resp.data


class TestAuditLogRedesign:
    def test_no_emoji_and_export_link_present(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin/audit-log')
        text = resp.data.decode()
        assert '⬇' not in text
        assert 'Export CSV' in text

    def test_empty_state(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin/audit-log')
        assert b'No admin actions logged yet' in resp.data


class TestDbDiagnosticStillWorks:
    def test_page_loads_with_correct_title(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin/db-diagnostic')
        assert resp.status_code == 200
        assert b'Database Migration Diagnostic' in resp.data

    def test_no_hardcoded_colors(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin/db-diagnostic')
        text = resp.data.decode()
        assert '#92400e' not in text
        assert '#f1f5f9' not in text

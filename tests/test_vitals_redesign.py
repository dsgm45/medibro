"""
Tests for the Vitals page redesign: Fraunces-styled snapshot numbers,
meaning-coded cards, and theme-aware charts (colors read via
getComputedStyle at draw time instead of hardcoded hex, so they match
whichever theme is active on page load).
"""
from datetime import datetime
import app as app_module
from conftest import login


class TestVitalsSnapshotUsesFraunces:
    def test_snapshot_numbers_use_fraunces_font(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            vital = app_module.Vital(patient_id=patient_id, systolic=118, diastolic=76, heart_rate=72, recorded_at=datetime.utcnow())
            app_module.db.session.add(vital)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/vitals')
        text = resp.data.decode()
        idx = text.find('118/76')
        assert "font-family: 'Fraunces'" in text[max(0, idx - 200):idx]


class TestVitalsChartsAreThemeAware:
    def test_chart_js_reads_colors_via_computed_style(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/vitals')
        text = resp.data.decode()
        assert 'getComputedStyle' in text
        assert "'--primary'" in text or '--primary' in text
        # No hardcoded hex chart colors left over from the old version
        assert '#2563eb' not in text
        assert '#f59e0b' not in text
        assert '#dc2626' not in text


class TestVitalsUsesThemeVariables:
    def test_no_hardcoded_light_mode_colors(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/vitals')
        text = resp.data.decode()
        assert '#0f172a' not in text
        assert '#cbd5e1' not in text
        assert 'var(--primary)' in text
        assert 'var(--surface)' in text


class TestVitalsExactSafetyCountPreserved:
    # The redesign must not have changed how many times "Not recorded"
    # appears for a patient with zero vitals - this is the same
    # safety-critical exact count locked in by the original missing
    # vitals safety fix.
    def test_no_vitals_shows_exactly_four_not_recorded(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/vitals')
        assert resp.data.count(b'Not recorded') == 4

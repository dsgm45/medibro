"""
Tests for three real bugs found after Batch 1 actually went live:
1. The Tabler icons CDN URL was genuinely broken (404), so every icon
   in the redesign was invisible - not a rendering choice, a dead link.
2. .mobile-more-sheet had no default display:none outside the mobile
   media query, so it rendered as an unstyled stack of plain links on
   desktop.
3. Batch 1 only touched the shared nav - individual page content
   (headers, buttons, labels) still had the old emoji throughout.
"""
import app as app_module
from conftest import login


class TestIconCdnIsCorrect:
    def test_uses_the_verified_working_cdn_url(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        assert b'cdn.jsdelivr.net/npm/@tabler/icons-webfont' in resp.data
        assert b'cdnjs.cloudflare.com/ajax/libs/tabler-icons' not in resp.data


class TestMoreSheetHiddenByDefault:
    def test_more_sheet_has_default_display_none_outside_media_query(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        text = resp.data.decode()
        # The default rule (outside any @media block) must exist, not
        # just the mobile-only override - otherwise this renders as a
        # visible, unstyled list of links on desktop.
        style_section = text[text.index('<style>'):text.index('@media (max-width: 640px)')]
        assert '.mobile-more-sheet' in style_section
        assert 'display: none' in style_section[style_section.index('.mobile-more-sheet'):]


class TestNoEmojiInPageContent:
    def test_my_health_has_no_emoji_anywhere(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        text = resp.data.decode()
        for emoji in ['👥', '📄', '🔍', '🤖', '📅', '💓', '🩺', '💊']:
            assert emoji not in text, f'{emoji} still found on My Health'

    def test_medicines_page_has_no_emoji_anywhere(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/medicines')
        text = resp.data.decode()
        for emoji in ['💊', '☀', '🌤', '🌙', '⚡', '✓', '➕', '📝', '📅']:
            assert emoji not in text, f'{emoji} still found on Medicines'

    def test_doctor_dashboard_has_no_emoji_anywhere(self, client, make_user):
        make_user('doc@example.com', 'password123', role='doctor', status='approved')
        login(client, 'doc@example.com', 'password123')
        resp = client.get('/doctor')
        text = resp.data.decode()
        for emoji in ['⚙', '📝', '💬', '🩺', '💊']:
            assert emoji not in text, f'{emoji} still found on Doctor Dashboard'

    def test_admin_dashboard_has_no_emoji_anywhere(self, client, make_user):
        make_user('admin@example.com', 'password123', role='hospital', status='approved')
        login(client, 'admin@example.com', 'password123')
        resp = client.get('/admin')
        text = resp.data.decode()
        for emoji in ['🚨', '👨', '🗑', '⚕', '📜', '👥']:
            assert emoji not in text, f'{emoji} still found on Admin Dashboard'


class TestTimePeriodIconLogic:
    def test_classify_time_period_returns_icon_names_not_emoji(self):
        period, icon = app_module.classify_time_period('08:00')
        assert period == 'Morning'
        assert icon == 'sunrise'
        assert not any(ord(c) > 0x2000 for c in icon)  # no emoji codepoints

    def test_classify_time_period_covers_all_periods(self):
        assert app_module.classify_time_period('08:00') == ('Morning', 'sunrise')
        assert app_module.classify_time_period('14:00') == ('Afternoon', 'sun')
        assert app_module.classify_time_period('20:00') == ('Evening', 'moon')
        assert app_module.classify_time_period('bad-input') == ('Other', 'clock')


class TestMyHealthUsesThemeVariables:
    def test_no_hardcoded_light_mode_colors_on_cards(self, client, make_user):
        # The whole point of the redesign is that dark mode should
        # actually work on page content, not just the shared nav - this
        # only holds if colors use CSS variables, not hardcoded hex.
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        text = resp.data.decode()
        assert 'color: #7a4a0f' not in text
        assert 'var(--primary)' in text
        assert 'var(--text-dark)' in text

    def test_vitals_numbers_use_fraunces(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            vital = app_module.Vital(patient_id=patient_id, heart_rate=72, recorded_at=app_module.datetime.utcnow())
            app_module.db.session.add(vital)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        text = resp.data.decode()
        idx = text.find('72')
        assert "font-family: 'Fraunces'" in text[max(0, idx-300):idx]

    def test_hero_card_has_flower_motif(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        doctor_id = make_user('doc@example.com', 'password123', role='doctor', status='approved')
        with app_module.app.app_context():
            appt = app_module.Appointment(
                patient_id=patient_id, doctor_id=doctor_id,
                appointment_date='2026-08-20', appointment_time='16:00',
                phone_number='555', status='accepted'
            )
            app_module.db.session.add(appt)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/my-health')
        assert b'<svg' in resp.data
        assert b'ellipse' in resp.data

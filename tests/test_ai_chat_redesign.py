"""
Tests for the AI Chat redesign: new palette applied, with special care
that the crisis-response message style stays clearly, visually distinct
from a normal AI reply - this is a safety-relevant distinction, not
just decoration, so a restyle must not accidentally make it blend in.
"""
from datetime import datetime
import app as app_module
from conftest import login


class TestAIChatUsesThemeVariables:
    def test_no_hardcoded_light_mode_colors(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/ai-chat')
        text = resp.data.decode()
        assert '#0f172a' not in text
        assert '#2563eb' not in text
        assert 'var(--primary)' in text

    def test_no_off_palette_purple_label(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            msg = app_module.AIChatMessage(patient_id=patient_id, sender='ai', content='General advice', is_crisis_response=False, created_at=datetime.utcnow())
            app_module.db.session.add(msg)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/ai-chat')
        text = resp.data.decode()
        assert '#6d28d9' not in text


class TestCrisisResponseStaysVisuallyDistinct:
    def test_crisis_message_uses_danger_colors(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            msg = app_module.AIChatMessage(patient_id=patient_id, sender='ai', content='Please reach out for help immediately.', is_crisis_response=True, created_at=datetime.utcnow())
            app_module.db.session.add(msg)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/ai-chat')
        text = resp.data.decode()
        idx = text.find('Please seek help')
        assert idx > -1
        preceding_block = text[max(0, idx - 400):idx]
        assert 'var(--danger-light)' in preceding_block
        assert 'var(--danger)' in preceding_block

    def test_normal_ai_message_does_not_use_danger_colors(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            msg = app_module.AIChatMessage(patient_id=patient_id, sender='ai', content='Rest and stay hydrated.', is_crisis_response=False, created_at=datetime.utcnow())
            app_module.db.session.add(msg)
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/ai-chat')
        text = resp.data.decode()
        idx = text.find('Rest and stay hydrated')
        preceding_block = text[max(0, idx - 400):idx]
        assert 'var(--danger-light)' not in preceding_block

    def test_crisis_and_normal_messages_together_remain_distinguishable(self, client, make_user):
        patient_id = make_user('patient@example.com', 'password123', role='patient')
        with app_module.app.app_context():
            m1 = app_module.AIChatMessage(patient_id=patient_id, sender='ai', content='Normal reply here', is_crisis_response=False, created_at=datetime.utcnow())
            m2 = app_module.AIChatMessage(patient_id=patient_id, sender='ai', content='Crisis reply here', is_crisis_response=True, created_at=datetime.utcnow())
            app_module.db.session.add_all([m1, m2])
            app_module.db.session.commit()

        login(client, 'patient@example.com', 'password123')
        resp = client.get('/ai-chat')
        text = resp.data.decode()
        # Scope to just the chat message area - base.html's own shared
        # nav also legitimately uses var(--danger-light) (e.g. the Log
        # Out button) AND has its own <form> tags, so both a whole-page
        # count and a generic "<form" boundary would be wrong here.
        chat_area = text[text.index('id="chat-scroll-area"'):text.index('placeholder="Ask a health question')]
        assert chat_area.count('var(--danger-light)') == 1  # exactly the one crisis message


class TestAIChatEmptyState:
    def test_no_messages_shows_empty_prompt(self, client, make_user):
        make_user('patient@example.com', 'password123', role='patient')
        login(client, 'patient@example.com', 'password123')
        resp = client.get('/ai-chat')
        assert b'No messages yet' in resp.data

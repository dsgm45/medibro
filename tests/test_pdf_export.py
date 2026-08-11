"""
Regression test for a real production bug: fpdf2 raises FPDFException
("Not enough horizontal space to render a single character") when it hits
any unbroken run of non-whitespace text wider than the page - a long URL,
a long word, or a long run of '?' characters produced by the existing
latin-1 fallback in _pdf_safe_text() when non-Latin text (e.g. Hindi/
Devanagari with no spaces) gets replaced character-by-character.

This tests _pdf_safe_text() directly as pure string logic - no need to
mock or install the real fpdf2 library, since the fix never touches
FPDF-specific code at all.
"""
import app as app_module


class TestPdfSafeTextWrapping:
    def test_normal_short_text_is_unchanged(self):
        text = 'This is a normal sentence with regular spacing.'
        assert app_module._pdf_safe_text(text) == text

    def test_none_returns_empty_string(self):
        assert app_module._pdf_safe_text(None) == ''

    def test_long_unbroken_word_gets_wrap_points(self):
        long_word = 'a' * 300
        result = app_module._pdf_safe_text(long_word)
        longest_run = max(len(w) for w in result.split(' '))
        assert longest_run <= 60

    def test_long_url_gets_wrap_points(self):
        long_url = 'https://example.com/' + 'x' * 300
        result = app_module._pdf_safe_text(long_url)
        longest_run = max(len(w) for w in result.split(' '))
        assert longest_run <= 60

    def test_long_run_of_non_latin1_characters_gets_wrap_points(self):
        # Simulates the real production case: non-Latin script text (e.g.
        # Hindi/Devanagari) with no natural spaces, which the latin-1
        # fallback turns into a long unbroken run of '?' characters.
        long_non_latin = '\u0939\u093f\u0902\u0926\u0940' * 100
        result = app_module._pdf_safe_text(long_non_latin)
        longest_run = max(len(w) for w in result.split(' '))
        assert longest_run <= 60
        assert '?' in result  # confirms the latin-1 fallback still applied

    def test_long_word_mixed_into_otherwise_normal_text(self):
        text = 'Patient reports ' + 'x' * 200 + ' after taking medication.'
        result = app_module._pdf_safe_text(text)
        longest_run = max(len(w) for w in result.split(' '))
        assert longest_run <= 60
        # The normal surrounding words should be untouched
        assert 'Patient' in result
        assert 'medication.' in result

    def test_accented_latin1_compatible_characters_pass_through(self):
        text = 'Caf\u00e9 r\u00e9sum\u00e9'
        result = app_module._pdf_safe_text(text)
        assert result == text  # no replacement needed, latin-1 already covers this

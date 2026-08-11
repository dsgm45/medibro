"""
Regression tests for a real production bug: fpdf2 raises FPDFException
("Not enough horizontal space to render a single character") - a known,
acknowledged bug in the fpdf2 library itself (see py-pdf/fpdf2#1250),
triggered by certain character sequences in its internal line-breaking
algorithm, most commonly (but not exclusively) long unbroken runs of
non-whitespace text.

Two layers are tested here:
1. _pdf_safe_text() - preventive text preprocessing (pure string logic,
   no fpdf2 needed to test).
2. _safe_pdf_multi_cell() - the real safety net: even if the preventive
   layer doesn't catch every case (this is an upstream bug whose exact
   trigger conditions aren't fully within this app's control), this
   wrapper ensures only one line degrades to a placeholder instead of the
   entire PDF export failing.
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
        assert longest_run <= 20

    def test_long_url_gets_wrap_points(self):
        long_url = 'https://example.com/' + 'x' * 300
        result = app_module._pdf_safe_text(long_url)
        longest_run = max(len(w) for w in result.split(' '))
        assert longest_run <= 20

    def test_long_run_of_non_latin1_characters_gets_wrap_points(self):
        # Simulates the real production case: non-Latin script text (e.g.
        # Hindi/Devanagari) with no natural spaces, which the latin-1
        # fallback turns into a long unbroken run of '?' characters.
        long_non_latin = '\u0939\u093f\u0902\u0926\u0940' * 100
        result = app_module._pdf_safe_text(long_non_latin)
        longest_run = max(len(w) for w in result.split(' '))
        assert longest_run <= 20
        assert '?' in result  # confirms the latin-1 fallback still applied

    def test_long_word_mixed_into_otherwise_normal_text(self):
        text = 'Patient reports ' + 'x' * 200 + ' after taking medication.'
        result = app_module._pdf_safe_text(text)
        longest_run = max(len(w) for w in result.split(' '))
        assert longest_run <= 20
        # The normal surrounding words should be untouched
        assert 'Patient' in result
        assert 'medication.' in result

    def test_accented_latin1_compatible_characters_pass_through(self):
        text = 'Caf\u00e9 r\u00e9sum\u00e9'
        result = app_module._pdf_safe_text(text)
        assert result == text  # no replacement needed, latin-1 already covers this


class _FakePdfThatAlwaysFails:
    """Simulates fpdf2's multi_cell raising an exception every time, to
    verify _safe_pdf_multi_cell() degrades gracefully. Uses a plain
    built-in exception rather than the real fpdf.errors.FPDFException,
    since _safe_pdf_multi_cell() catches broad Exception (not that
    specific type) and this way the test doesn't require the real fpdf2
    package to be installed to run - same lesson learned from the
    google-genai testing situation earlier."""
    def __init__(self):
        self.calls = []

    def multi_cell(self, w, h, text):
        self.calls.append(text)
        raise RuntimeError("Not enough horizontal space to render a single character")


class _FakePdfThatSucceeds:
    def __init__(self):
        self.calls = []

    def multi_cell(self, w, h, text):
        self.calls.append(text)


class TestSafePdfMultiCell:
    def test_successful_render_passes_through_unchanged(self):
        fake_pdf = _FakePdfThatSucceeds()
        app_module._safe_pdf_multi_cell(fake_pdf, 0, 6, 'A normal line of text.')
        assert fake_pdf.calls == ['A normal line of text.']

    def test_failure_falls_back_to_placeholder_instead_of_raising(self):
        fake_pdf = _FakePdfThatAlwaysFails()
        # Should NOT raise - this is the whole point of the wrapper
        app_module._safe_pdf_multi_cell(fake_pdf, 0, 6, 'Problematic text')
        # First call attempted the real text, second call was the placeholder
        assert len(fake_pdf.calls) == 2
        assert fake_pdf.calls[0] == 'Problematic text'
        assert 'could not be displayed' in fake_pdf.calls[1]

    def test_even_placeholder_failure_does_not_raise(self):
        class _FakePdfThatAlwaysFailsEvenPlaceholder:
            def multi_cell(self, w, h, text):
                raise RuntimeError("Not enough horizontal space to render a single character")

        fake_pdf = _FakePdfThatAlwaysFailsEvenPlaceholder()
        # Should not raise even when the fallback attempt ALSO fails
        app_module._safe_pdf_multi_cell(fake_pdf, 0, 6, 'Problematic text')

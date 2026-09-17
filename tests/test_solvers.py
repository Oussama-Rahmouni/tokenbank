from tokenbank.solvers import (
    CHECKBOX_PATTERNS,
    CHECKED_SELECTORS,
    FAIL_URL_MARKERS,
    BrowserSolver,
)


class FakePage:
    def __init__(self, url: str):
        self.url = url


# ── _resolved: title-based challenge detection ──────────────────────────────

def test_resolved_on_normal_titles():
    assert BrowserSolver._resolved("Example Domain") is True
    assert BrowserSolver._resolved("") is True
    assert BrowserSolver._resolved("  My Shop — Checkout  ") is True


def test_unresolved_on_challenge_titles():
    assert BrowserSolver._resolved("Just a moment...") is False
    assert BrowserSolver._resolved("just a moment") is False
    assert BrowserSolver._resolved("Verify you are human") is False
    assert BrowserSolver._resolved("Attention Required! | Cloudflare") is False


# ── _looks_flagged via FAIL_URL_MARKERS ─────────────────────────────────────

def test_looks_flagged_on_fail_url():
    solver = BrowserSolver()
    for marker in FAIL_URL_MARKERS:
        page = FakePage(f"https://{marker}/something")
        assert solver._looks_flagged(page, "<html>real-ish body</html>") is True


def test_looks_flagged_interstitial_shell():
    solver = BrowserSolver()
    page = FakePage("https://example.com/")
    shell = "<html>cf-browser-verification challenge-platform __cf_chl</html>"
    assert solver._looks_flagged(page, shell) is True


def test_not_flagged_on_real_page():
    solver = BrowserSolver()
    page = FakePage("https://example.com/products")
    body = "<html>" + "real content " * 10_000 + "</html>"
    assert solver._looks_flagged(page, body) is False


def test_looks_flagged_handles_broken_page_url():
    class BrokenPage:
        @property
        def url(self):
            raise RuntimeError("page gone")

    solver = BrowserSolver()
    # url inaccessible, body clean → not flagged
    assert solver._looks_flagged(BrokenPage(), "<html>ok</html>") is False


# ── CHECKBOX_PATTERNS sanity ────────────────────────────────────────────────

def test_checkbox_patterns_structure():
    kinds = [kind for _frame, _inner, kind in CHECKBOX_PATTERNS]
    assert kinds == ["turnstile", "recaptcha", "hcaptcha", "funcaptcha"]
    for frame_match, inner, kind in CHECKBOX_PATTERNS:
        assert frame_match.startswith("iframe[src*=")
        # selectors must be lowercase — CSS attribute matching is case-sensitive
        assert frame_match == frame_match.lower()
        # every kind must have a CHECKED_SELECTORS entry (selector or None)
        assert kind in CHECKED_SELECTORS


def test_inner_selectors_target_the_checkbox():
    by_kind = {kind: inner for _f, inner, kind in CHECKBOX_PATTERNS}
    assert by_kind["recaptcha"] == ".recaptcha-checkbox-border"
    assert by_kind["hcaptcha"] == ".h-captcha-checkbox"
    # turnstile/funcaptcha: click the frame root, no inner selector
    assert by_kind["turnstile"] is None
    assert by_kind["funcaptcha"] is None


# ── header injector ─────────────────────────────────────────────────────────

def test_header_injector_builds_js():
    js = BrowserSolver._header_injector({"X-Test": "abc", "Accept-Language": "en-US"})
    assert js.startswith("(() => {")
    assert js.endswith("})();")
    assert "'X-Test': 'abc'" in js
    assert "'Accept-Language': 'en-US'" in js
    assert "XMLHttpRequest.prototype.open" in js
    assert "window.fetch" in js


def test_header_injector_empty():
    js = BrowserSolver._header_injector({})
    assert "const h = {  };" in js


def test_header_injector_quotes_values():
    js = BrowserSolver._header_injector({"X-Q": "it's"})
    assert repr("it's") in js

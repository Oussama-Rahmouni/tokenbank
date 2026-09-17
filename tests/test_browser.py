from tokenbank.browser import (
    BrowserSession,
    SessionConfig,
    has_challenge_marker,
    is_interstitial,
)


# ── challenge marker logic ──────────────────────────────────────────────────

def test_has_challenge_marker():
    assert has_challenge_marker("<title>Just a moment...</title>") is True
    assert has_challenge_marker("Please enable JavaScript to continue") is True
    assert has_challenge_marker("window._px = {}") is True
    assert has_challenge_marker("<html><body>Normal product page</body></html>") is False


def test_has_challenge_marker_case_insensitive():
    assert has_challenge_marker("TURNSTILE widget embedded") is True


def test_is_interstitial():
    assert is_interstitial("cf-browser-verification") is True
    assert is_interstitial("challenge-platform/v1/turnstile") is True
    assert is_interstitial("Checking your browser before accessing") is True
    # a real page that merely embeds a widget is not an interstitial
    assert is_interstitial("<html>Login form with a turnstile widget</html>") is False


# ── _context_options (no browser started) ───────────────────────────────────

def make_session(**kwargs) -> BrowserSession:
    return BrowserSession(SessionConfig(**kwargs))


def test_context_options_defaults():
    cfg = SessionConfig(
        engine="patchright", channel="chrome", headless=True, mobile=False,
        locale="en-US", timezone="America/New_York", viewport=(1366, 850),
        user_agent="UA-TEST", proxy=None,
    )
    opts = BrowserSession(cfg)._context_options()
    assert opts["viewport"] == {"width": 1366, "height": 850}
    assert opts["locale"] == "en-US"
    assert opts["timezone_id"] == "America/New_York"
    assert opts["user_agent"] == "UA-TEST"
    assert "proxy" not in opts
    assert "is_mobile" not in opts


def test_context_options_proxy_with_credentials():
    session = make_session(
        engine="patchright", proxy="http://user:pass@proxy.example:8080",
    )
    opts = session._context_options()
    assert opts["proxy"] == {
        "server": "http://proxy.example:8080",
        "username": "user",
        "password": "pass",
    }


def test_context_options_proxy_without_credentials():
    session = make_session(engine="patchright", proxy="socks5://10.0.0.1:1080")
    opts = session._context_options()
    assert opts["proxy"] == {"server": "socks5://10.0.0.1:1080"}
    assert "username" not in opts["proxy"]
    assert "password" not in opts["proxy"]


def test_context_options_proxy_ignored_for_camoufox():
    # camoufox takes the proxy at launch time, not in context options
    session = make_session(engine="camoufox", proxy="http://user:pass@proxy.example:8080")
    opts = session._context_options()
    assert "proxy" not in opts


def test_context_options_mobile():
    session = make_session(engine="patchright", mobile=True, user_agent=None)
    opts = session._context_options()
    assert opts["viewport"] == {"width": 390, "height": 844}
    assert opts["is_mobile"] is True
    assert opts["has_touch"] is True
    assert "iPhone" in opts["user_agent"]


def test_is_persistent():
    assert make_session(profile_dir="/tmp/id1").is_persistent() is True
    assert make_session().is_persistent() is False

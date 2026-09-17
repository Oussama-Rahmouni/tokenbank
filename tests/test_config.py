import json

from tokenbank.config import Settings


def test_defaults(monkeypatch):
    for var in list(_tokenbank_vars()):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.host == "127.0.0.1"
    assert s.port == 8123
    assert s.engine == "patchright"
    assert s.channel == "chrome"
    assert s.headless is True
    assert s.mobile is False
    assert s.proxy is None
    assert s.token_ttl_s == 3600
    assert s.presolve_targets == []


def test_env_prefix_parsing(monkeypatch):
    monkeypatch.setenv("TOKENBANK_HOST", "0.0.0.0")
    monkeypatch.setenv("TOKENBANK_PORT", "9999")
    monkeypatch.setenv("TOKENBANK_HEADLESS", "0")
    monkeypatch.setenv("TOKENBANK_MOBILE", "1")
    monkeypatch.setenv("TOKENBANK_PROXY", "http://127.0.0.1:8080")
    monkeypatch.setenv("TOKENBANK_TOKEN_TTL", "120")
    s = Settings()
    assert s.host == "0.0.0.0"
    assert s.port == 9999
    assert s.headless is False
    assert s.mobile is True
    assert s.proxy == "http://127.0.0.1:8080"
    assert s.token_ttl_s == 120


def test_unrelated_prefix_has_no_effect(monkeypatch):
    monkeypatch.setenv("LEGACY_PORT", "4444")
    monkeypatch.delenv("TOKENBANK_PORT", raising=False)
    assert Settings().port == 8123


def test_viewport_parsing(monkeypatch):
    monkeypatch.setenv("TOKENBANK_VIEWPORT", "1920x1080")
    assert Settings().viewport_wh == (1920, 1080)


def test_viewport_fallback_on_garbage(monkeypatch):
    monkeypatch.setenv("TOKENBANK_VIEWPORT", "not-a-viewport")
    assert Settings().viewport_wh == (1366, 850)
    monkeypatch.setenv("TOKENBANK_VIEWPORT", "1920")
    assert Settings().viewport_wh == (1366, 850)


def test_presolve_json_loading(monkeypatch):
    targets = [{"url": "https://example.com/", "domain": "example.com", "min_tokens": 3}]
    monkeypatch.setenv("TOKENBANK_PRESOLVE", json.dumps(targets))
    assert Settings().presolve_targets == targets


def test_presolve_invalid_json(monkeypatch):
    monkeypatch.setenv("TOKENBANK_PRESOLVE", "{not json")
    assert Settings().presolve_targets == []
    monkeypatch.setenv("TOKENBANK_PRESOLVE", '{"not": "a list"}')
    assert Settings().presolve_targets == []


def test_state_dir_default(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENBANK_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    s = Settings()
    assert s.state_dir == tmp_path / ".tokenbank"


def _tokenbank_vars():
    import os

    return [k for k in os.environ if k.startswith("TOKENBANK_")]

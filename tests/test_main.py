from tokenbank.main import _base_domain, _cookies_for_domain


def test_base_domain():
    assert _base_domain("example.com") == "example.com"
    assert _base_domain("www.example.com") == "example.com"
    assert _base_domain("a.b.example.com") == "example.com"
    assert _base_domain("localhost") == "localhost"


def test_cookies_for_domain_keeps_own_and_subdomains():
    cookies = [
        {"name": "cf_clearance", "value": "x", "domain": ".example.com"},
        {"name": "session", "value": "y", "domain": "app.example.com"},
        {"name": "root", "value": "z", "domain": "example.com"},
    ]
    kept = _cookies_for_domain(cookies, "www.example.com")
    assert [c["name"] for c in kept] == ["cf_clearance", "session", "root"]


def test_cookies_for_domain_drops_foreign():
    cookies = [
        {"name": "tracker", "value": "t", "domain": ".doubleclick.net"},
        {"name": "cdn", "value": "c", "domain": "example-cdn.com"},
        {"name": "sneaky", "value": "s", "domain": "notexample.com"},
        {"name": "good", "value": "g", "domain": ".example.com"},
    ]
    kept = _cookies_for_domain(cookies, "example.com")
    assert [c["name"] for c in kept] == ["good"]


def test_cookies_for_domain_case_and_dot_normalisation():
    cookies = [
        {"name": "a", "value": "1", "domain": ".EXAMPLE.com"},
        {"name": "b", "value": "2", "domain": ""},  # empty domain → dropped
    ]
    kept = _cookies_for_domain(cookies, "Example.COM")
    assert [c["name"] for c in kept] == ["a"]

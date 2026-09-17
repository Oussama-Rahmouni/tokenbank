import pytest

from tokenbank.tokenbank import TokenBank


@pytest.fixture
async def bank(tmp_path):
    b = TokenBank(tmp_path / "bank.db")
    await b.init()
    yield b
    await b.close()


COOKIES = [{"name": "cf_clearance", "value": "abc123", "domain": ".example.com"}]


async def test_mint_acquire_release_reacquire(bank):
    token_id = await bank.mint("example.com", COOKIES, ttl_s=3600)
    assert token_id

    token = await bank.acquire("example.com")
    assert token is not None
    assert token.token_id == token_id
    assert token.cookies == COOKIES

    # rented → not acquirable again until released
    assert await bank.acquire("example.com") is None

    assert await bank.release(token_id) is True
    token2 = await bank.acquire("example.com")
    assert token2 is not None
    assert token2.token_id == token_id


@pytest.mark.parametrize("ttl", [0, -1])
async def test_expired_token_not_acquirable(bank, ttl):
    await bank.mint("example.com", COOKIES, ttl_s=ttl)
    assert await bank.acquire("example.com") is None


async def test_sweep_marks_expired(bank):
    await bank.mint("example.com", COOKIES, ttl_s=0)
    await bank.mint("example.com", COOKIES, ttl_s=3600)

    assert await bank.sweep() == 1

    stats = await bank.stats()
    assert stats.get("expired") == 1
    assert stats.get("available") == 1


async def test_proxy_key_stickiness(bank):
    await bank.mint("example.com", COOKIES, proxy_key="http://proxy-a:8080", ttl_s=3600)

    # wrong proxy key → no token
    assert await bank.acquire("example.com", proxy_key="http://proxy-b:8080") is None
    # no proxy key requested → any available token matches
    assert await bank.acquire("example.com") is not None

    await bank.mint("other.com", COOKIES, proxy_key="http://proxy-a:8080", ttl_s=3600)
    token = await bank.acquire("other.com", proxy_key="http://proxy-a:8080")
    assert token is not None
    assert token.proxy_key == "http://proxy-a:8080"


async def test_count_available(bank):
    assert await bank.count_available("example.com") == 0
    await bank.mint("example.com", COOKIES, ttl_s=3600)
    await bank.mint("example.com", COOKIES, ttl_s=3600)
    await bank.mint("example.com", COOKIES, ttl_s=0)  # expired — not counted
    assert await bank.count_available("example.com") == 2
    assert await bank.count_available("other.com") == 0

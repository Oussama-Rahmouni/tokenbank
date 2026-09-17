# tokenbank

Solve a bot challenge once, replay the clearance a thousand times — a real-browser solver farm with a warm token bank.

## The problem

Browser-based challenge solving does not scale as a hot path. If every blocked request gets its own browser session, a 100k-item crawl costs 100k solves — minutes of browser time each, plus a fresh fingerprint per request that looks nothing like a returning user.

## The concept

**Solve once, reuse everywhere.** A farm of real browsers (patchright / camoufox) works through invisible and checkbox challenges — Cloudflare, Turnstile, PerimeterX sensor, Akamai `_abck`, reCAPTCHA/hCaptcha checkbox — and mints the resulting clearance cookies (`cf_clearance`, `_abck`, …) into a token bank: a SQLite pool of solved challenges, keyed by domain.

Tokens are **identity-sticky**: each carries the user agent and proxy key it was minted on, because modern WAF tokens are bound to IP + UA + TLS fingerprint. Rent a token, replay it from the same exit IP with a coherent client (e.g. curl-impersonate), and you get plain-HTTP throughput against a challenge-protected site. The browser becomes an exception handler, not the hot path.

The amortization, measured by the bundled benchmark:

```
one solve (6.2s) enabled 300 HTTP requests before re-challenge
amortization: ~300 req/solve at 500ms delay
100k items → ~334 solves
  ≈ 34 min of single-browser farm time
  ≈ 3 min with a 10-browser farm
```

## Install

```bash
pip install .
# patchright drives the installed Chrome (channel=chrome); for camoufox instead:
pip install '.[camoufox]' && python -m camoufox fetch
```

## Quickstart

```bash
tokenbank-server        # serves on 127.0.0.1:8123 by default

# solve a challenge, get back the page + cookies
curl -s localhost:8123/v1/solve \
  -H 'content-type: application/json' \
  -d '{"url": "https://example.com/", "mode": "page"}'

# rent a pre-solved token for a domain
curl -s localhost:8123/v1/token/example.com

# return it when done
curl -s -X POST localhost:8123/v1/token/<token_id>/release
```

> The console script forces `--loop asyncio`. `uvicorn[standard]` defaults to uvloop,
> which deadlocks playwright/patchright's greenlet bridge — solves hang forever. If you
> run uvicorn yourself, pass `--loop asyncio`.

## API

| Endpoint | Purpose |
|---|---|
| `POST /v1/solve` | `{url, mode: page\|tokens, cookies?, headers?, proxy?, engine?, domain?, profile?}` → solved page + cookies; with `domain`, also mints a token into the bank |
| `GET /v1/token/{domain}?proxy=` | rent a warm pre-solved token (IP-sticky by proxy key) |
| `POST /v1/token/{id}/release` | return a token to the pool |
| `POST /v1/token/sweep` | expire stale tokens |
| `GET /v1/health` | liveness + pool/bank/presolve stats |

**Burn-on-flag policy:** if an identity is downgraded to an image-grid captcha, the solve is reported `flagged`, the identity is retired from the pool, and the next identity takes over — keeping "checkbox-only" true by construction.

## Probe (self-test)

RoE-clean: all default targets are the vendors' own public demo pages (no auth, no data, no ToS risk).

```bash
tokenbank-probe                 # turnstile + recaptcha v2 + hcaptcha demos
tokenbank-probe --url https://example.com --label custom --verbose
```

## Benchmark

Measures the reuse ratio on a real target: one solve, then plain-HTTP replays over curl-impersonate until the WAF re-challenges. Requires the server running and a `curl-impersonate-chrome` binary (set `CURL_IMPERSONATE_BIN` or put it on PATH).

```bash
tokenbank-bench --url https://your-target.example/ --requests 300 --delay-ms 500
```

## Honest caveats

- **Invisible challenges pass for free** — Turnstile non-interactive, CF "Just a moment", PX/Akamai sensor. Proven against Cloudflare's own demo.
- **Checkbox pass rate is not 100%** and is per-site-key. Google's public reCAPTCHA demo key is a bot honeypot that serves an image challenge to automated browsers *even headed*; production keys with a clean real Chrome + residential IP pass at much higher rates. The burn-on-flag pool + identity-per-proxy profiles are the mitigation.
- **Strongest mode is headed under xvfb** (`TOKENBANK_HEADLESS=0` + Xvfb). Headless-new is the fast default; use headed for the hardest targets.

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `TOKENBANK_HOST` / `TOKENBANK_PORT` | `127.0.0.1` / `8123` | service bind |
| `TOKENBANK_ENGINE` | `patchright` | `patchright` or `camoufox` (needs the `camoufox` extra + `python -m camoufox fetch`) |
| `TOKENBANK_CHANNEL` | `chrome` | drives installed Chrome; `""` → patchright-bundled chromium (stealthier on some sites) |
| `TOKENBANK_HEADLESS` | `1` | `0` → headed (strongest; run under xvfb) |
| `TOKENBANK_MOBILE` | `0` | `1` → mobile emulation (iPhone-class viewport + touch + mobile UA) |
| `TOKENBANK_PROXY` | — | route the browser through an upstream exit (`http://` / `socks5://`, credentials in URL supported) |
| `TOKENBANK_POOL_SIZE` | `2` | farm session ceiling |
| `TOKENBANK_SOLVE_TIMEOUT` | `75` | per-solve timeout (s) |
| `TOKENBANK_MAX_BODY` | `2000000` | max response body bytes returned |
| `TOKENBANK_TOKEN_TTL` | `3600` | token lifetime (s) |
| `TOKENBANK_PRESOLVE` | — | JSON `[{"url": "...", "domain": "...", "min_tokens": 3}]` — warm-mint tokens ahead of demand |
| `TOKENBANK_PRESOLVE_INTERVAL` | `600` | presolve loop interval (s) |
| `TOKENBANK_STATE_DIR` | `~/.tokenbank` | identity profiles + token bank db |
| `TOKENBANK_LOCALE` / `TOKENBANK_TIMEZONE` | `en-US` / `America/New_York` | browser identity |
| `TOKENBANK_VIEWPORT` | `1366x850` | `WIDTHxHEIGHT` |
| `TOKENBANK_USER_AGENT` | real Chrome desktop UA | override the UA (keep it coherent with the channel build) |
| `TOKENBANK_AGING_INTERVAL` / `TOKENBANK_AGING_URLS` | `600` / — | persistent profiles periodically browse benign pages so they accumulate real history |

## Scope & ethics

tokenbank solves challenges on sites you are authorized to crawl, at request rates the target can absorb. The default probe targets are vendor demo pages published for exactly this purpose. Tokens are replayed from the same IP and fingerprint that earned them — the tool exists to amortize legitimate solves, not to multiply identities. You are responsible for complying with the target's terms and applicable law.

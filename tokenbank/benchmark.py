"""Token amortization benchmark — how many HTTP requests does ONE solve enable?

The scale answer for large crawls: browser solving must be an exception handler, not the
hot path. This measures the reuse ratio on a real challenge-protected target:

  1. Solve the target via tokenbank (mints cf_clearance/_abck/... cookies).
  2. Replay those cookies over plain HTTP from the SAME IP (the token is IP-sticky).
  3. Count requests that pass before the WAF re-challenges.

Run with the tokenbank server up:
  tokenbank-bench --url https://example.com/ --requests 300
"""

import argparse
import json
import os
import subprocess
import time
import urllib.request
from urllib.parse import urlparse

# Coherent replay: curl-impersonate replays Chrome's TLS/HTTP2 fingerprint. The solve must
# use the SAME UA so the token (IP+UA+TLS bound) survives replay. Override with
# CURL_IMPERSONATE_BIN; otherwise the binary is resolved from PATH.
DEFAULT_CURL_BIN = os.environ.get("CURL_IMPERSONATE_BIN", "curl-impersonate-chrome")
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
)


def solve(url: str, tokenbank_url: str = "http://127.0.0.1:8123") -> tuple[dict, list[dict]]:
    body = json.dumps({"url": url, "mode": "tokens"}).encode()
    req = urllib.request.Request(
        f"{tokenbank_url}/v1/solve", data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    if not data.get("ok") or not data.get("cookies"):
        raise RuntimeError(f"solve failed: {data.get('error')}")
    return data, data["cookies"]


def cookie_header(cookies: list[dict]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))


def fetch(url: str, cookie: str, ua: str, curl_bin: str) -> int:
    """Replay over curl-impersonate — the TLS-coherent HTTP path."""
    try:
        out = subprocess.run(
            [
                curl_bin,
                "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "-H", f"User-Agent: {ua}",
                "-H", f"Cookie: {cookie}",
                "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "--http2", "--compressed",
                url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        return int(out.stdout.strip() or 0)
    except Exception:
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Token amortization benchmark: one solve, N replays over curl-impersonate."
    )
    parser.add_argument("--url", required=True, help="challenge-protected target URL")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--delay-ms", type=float, default=500.0, help="delay between requests (low & slow)")
    parser.add_argument("--curl-bin", default=DEFAULT_CURL_BIN)
    args = parser.parse_args()

    print(f"target: {args.url}  requests: {args.requests}  delay: {args.delay_ms}ms")
    print(f"replay transport: curl-impersonate ({args.curl_bin})\n")

    # Control group — coherent TLS, no token.
    print("control: curl-impersonate with NO token ...")
    control = fetch(args.url, "", DEFAULT_UA, args.curl_bin)
    print(f"  → HTTP {control} (expect 4xx if the WAF blocks coherent TLS too)\n")

    # One solve.
    print("solving once via tokenbank ...")
    t0 = time.monotonic()
    data, cookies = solve(args.url)
    solve_s = time.monotonic() - t0
    names = [c["name"] for c in cookies]
    print(f"  → solved in {solve_s:.1f}s, {len(cookies)} cookies ({', '.join(n[:12] for n in names[:8])}…)\n")
    token = cookie_header(cookies)
    ua = data.get("user_agent") or DEFAULT_UA

    # Replay.
    print("replaying the token over curl-impersonate (coherent TLS) ...")
    passed = 0
    first_block_at = None
    for i in range(1, args.requests + 1):
        status = fetch(args.url, token, ua, args.curl_bin)
        if status in (200, 201, 202, 204):
            passed += 1
        else:
            first_block_at = i
            print(f"  → re-challenged at request #{i} (HTTP {status})")
            break
        if i % 50 == 0:
            print(f"  → {i}/{args.requests} OK")
        time.sleep(args.delay_ms / 1000.0)

    if first_block_at is None:
        first_block_at = args.requests + 1

    print("\n── RESULT ────────────────────────────────────────────────")
    print(f"one solve ({solve_s:.1f}s) enabled {passed} HTTP requests before re-challenge")
    if passed > 0:
        print(f"amortization: ~{passed} req/solve at {args.delay_ms}ms delay")
        print(f"100k items → ~{round(100_000 / passed)} solves")
        print(f"  ≈ {round(100_000 / passed * solve_s / 60)} min of single-browser farm time")
        print(f"  ≈ {round(100_000 / passed * solve_s / 60 / 10)} min with a 10-browser farm")
    else:
        print("amortization: 0 — replay did not pass even once (check coherence / IP bind)")
    print("──────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()

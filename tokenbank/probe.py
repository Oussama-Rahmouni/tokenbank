"""Self-test probe — validates tokenbank against RoE-clean vendor demo pages.

All default targets are the vendors' OWN public demo pages (no auth, no data, no ToS risk):
  - https://demo.turnstile.workers.dev/        Cloudflare Turnstile demo
  - https://www.google.com/recaptcha/api2/demo  Google reCAPTCHA v2 demo
  - https://accounts.hcaptcha.com/demo          hCaptcha demo

Usage:
  tokenbank-probe [--url ...] [--mode page|tokens] [--verbose]
"""

import argparse
import asyncio
import json
import logging
import time

from .browser import BrowserSession, SessionConfig
from .solvers import SolveRequest, BrowserSolver

DEFAULT_TARGETS = [
    ("turnstile", "https://demo.turnstile.workers.dev/"),
    ("recaptcha-v2", "https://www.google.com/recaptcha/api2/demo"),
    ("hcaptcha", "https://accounts.hcaptcha.com/demo"),
]


async def probe_one(label: str, url: str, mode: str, verbose: bool) -> dict:
    solver = BrowserSolver()
    session = BrowserSession(SessionConfig())
    await session.start()
    try:
        result = await solver.solve(SolveRequest(url=url, mode=mode, timeout_s=90), session)
        verdict = "PASS" if result.ok and not result.flagged else "FLAG" if result.flagged else "FAIL"
        out = {
            "target": label,
            "verdict": verdict,
            "status": result.status,
            "cookies": len(result.cookies),
            "body_bytes": len(result.body),
            "duration_ms": result.duration_ms,
            "error": result.error,
            "user_agent": result.user_agent,
        }
        if verbose:
            out["cookie_names"] = [c.get("name") for c in result.cookies][:20]
            out["final_url"] = result.url
            out["body_preview"] = result.body[:300]
        return out
    finally:
        await session.close()


async def _run() -> None:
    parser = argparse.ArgumentParser(description="tokenbank self-test probe")
    parser.add_argument("--url", help="single target to probe (instead of defaults)")
    parser.add_argument("--label", default="custom")
    parser.add_argument("--mode", default="tokens", choices=["page", "tokens"])
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit results as JSON")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    targets = [(args.label, args.url)] if args.url else DEFAULT_TARGETS
    results = [await probe_one(label, url, args.mode, args.verbose) for label, url in targets]

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for r in results:
            print(f"[{r['verdict']:>4}] {r['target']:12} status={r['status']} "
                  f"cookies={r['cookies']} {r['duration_ms']}ms  {r['error'] or ''}")
    passed = sum(1 for r in results if r["verdict"] == "PASS")
    print(f"\n{passed}/{len(results)} targets passed")
    raise SystemExit(0 if passed == len(results) else 1)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()

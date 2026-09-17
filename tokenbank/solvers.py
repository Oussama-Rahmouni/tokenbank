"""Solver layer — pluggable, browser-first.

The core insight: invisible and checkbox challenges (Cloudflare, Turnstile invisible,
PerimeterX sensor, Akamai _abck, reCAPTCHA/hCaptcha checkbox) are beaten by running a
clean real browser — no ML, no solver service, no human. The `browser` backend is the
only one built; `service`/`cv`/`human` exist as interface stubs so other backends can
slot in without touching callers.

The checkbox click is best-effort: with a clean fingerprint most widgets auto-pass
(Turnstile non-interactive). If a widget downgrades to an image grid, the solve is
reported as flagged and the caller retires that identity (burn-on-flag policy).
"""

import asyncio
import random
import time
from dataclasses import dataclass, field

from .browser import BrowserSession, is_interstitial

# (frame src match, element selector, kind) tried in order. Selectors are src-based and
# lowercase (CSS attribute matching is case-sensitive — `title*='recaptcha'` misses
# `reCAPTCHA`). Clicking the widget is enough for clean browsers.
CHECKBOX_PATTERNS = [
    ("iframe[src*='challenges.cloudflare.com']", None, "turnstile"),
    ("iframe[src*='recaptcha/api2/anchor']", ".recaptcha-checkbox-border", "recaptcha"),
    ("iframe[src*='hcaptcha.com']", ".h-captcha-checkbox", "hcaptcha"),
    ("iframe[src*='funcaptcha']", None, "funcaptcha"),
]

# Post-click state selectors that prove the widget accepted the click (checked, not grid).
CHECKED_SELECTORS = {
    "turnstile": "iframe[src*='challenges.cloudflare.com']",
    "recaptcha": ".recaptcha-checkbox-checked",
    "hcaptcha": ".h-captcha-checkbox--checked",
    "funcaptcha": None,
}

SUCCESS_TITLE_MARKERS = ("just a moment", "verify you are human", "attention required")

# Pages that are pure challenges or need a login wall — never a success state.
FAIL_URL_MARKERS = ("challenges.cloudflare.com", "accounts.google.com/signin")


@dataclass
class SolveRequest:
    url: str
    mode: str = "page"  # page → return rendered response; tokens → return cookies only
    cookies: list[dict] = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    proxy: str | None = None
    engine: str | None = None
    timeout_s: int | None = None


@dataclass
class SolveResult:
    ok: bool
    url: str = ""
    status: int | None = None
    headers: dict = field(default_factory=dict)
    body: str = ""
    cookies: list[dict] = field(default_factory=list)
    user_agent: str = ""
    token_id: str | None = None
    flagged: bool = False
    error: str | None = None
    duration_ms: int = 0
    mode: str = "page"


class Solver:
    """Pluggable solver backend interface."""

    name = "base"

    async def solve(self, req: SolveRequest, session: BrowserSession) -> SolveResult:  # pragma: no cover
        raise NotImplementedError


def _jitter(lo: float = -6.0, hi: float = 6.0) -> float:
    return random.uniform(lo, hi)


class BrowserSolver(Solver):
    """Drives a real browser through the challenge and extracts cookies + page."""

    name = "browser"

    async def solve(self, req: SolveRequest, session: BrowserSession) -> SolveResult:
        start = time.monotonic()
        ctx = None
        page = None
        try:
            # Persistent identity (one profile = one target): reuse the shared context so
            # cookies/history accumulate across solves. Ephemeral pool sessions: fresh
            # context per solve → no cross-target contamination.
            if session.is_persistent():
                page = await session.new_page()
            else:
                ctx = await session.new_context()
                page = await ctx.new_page()
            await self._apply_request(page, req)

            timeout = req.timeout_s or 75
            status: int | None = None
            try:
                response = await page.goto(req.url, wait_until="domcontentloaded", timeout=timeout * 1000)
                status = response.status if response is not None else None
            except Exception as e:  # navigation can throw on 3xx/abort — keep going
                self._trace(session, f"goto note: {e}")

            await self._wait_and_pass(page, timeout)
            if status is None:
                status = await self._safe_status(page)

            body = await self._safe_body(page)
            cookies = await self._safe_cookies(page)
            final_url = page.url
            ua = await self._safe_ua(page)

            flagged = self._looks_flagged(page, body)
            # A real page has real content; a challenge shell is tiny. Reject shells.
            ok = not flagged and status in (200, 201, 202, 204) and len(body) >= 2000

            return SolveResult(
                ok=ok,
                url=final_url,
                status=status,
                headers={"user-agent": ua},
                body=body[: 2_000_000],
                cookies=cookies,
                user_agent=ua,
                flagged=flagged,
                error=None if ok else self._describe_failure(page, body),
                duration_ms=int((time.monotonic() - start) * 1000),
                mode=req.mode,
            )
        except Exception as e:  # noqa: BLE001
            return SolveResult(
                ok=False,
                url=(page.url if page is not None else req.url),
                error=f"browser error: {e}",
                duration_ms=int((time.monotonic() - start) * 1000),
                mode=req.mode,
            )
        finally:
            await self._close_page(page)
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass

    # ── internals ───────────────────────────────────────────────────────────
    async def _apply_request(self, page, req: SolveRequest) -> None:
        if req.headers:
            await page.context.add_init_script(
                self._header_injector(req.headers)
            )
        if req.cookies:
            for c in req.cookies:
                await page.context.add_cookies([c])

    @staticmethod
    def _header_injector(headers: dict) -> str:
        items = ", ".join(f"{k!r}: {v!r}" for k, v in headers.items())
        return (
            "(() => { const h = { %s }; "
            "const o = XMLHttpRequest.prototype.open; "
            "const s = XMLHttpRequest.prototype.setRequestHeader; "
            "XMLHttpRequest.prototype.open = function(...a){ this.__h=Object.assign({},h); return o.apply(this,a); }; "
            "XMLHttpRequest.prototype.setRequestHeader = function(k,v){ return s.call(this,k,v); }; "
            "const g = window.fetch; window.fetch = function(u,o={}){ o.headers = Object.assign({}, h, o.headers||{}); return g(u,o); }; })();"
        ) % items

    async def _wait_and_pass(self, page, timeout_s: int) -> None:
        """Wait for the challenge to resolve; click a checkbox if one appears."""
        deadline = time.monotonic() + timeout_s
        await asyncio.sleep(1.5)  # initial settle + let JS challenges run
        while time.monotonic() < deadline:
            title = await self._safe_title(page)
            if self._resolved(title):
                return
            verified = await self._try_click_checkbox(page)
            if verified:
                await asyncio.sleep(2.0)  # let the token land and the page finish
                return
            await asyncio.sleep(0.8)

    async def _try_click_checkbox(self, page) -> bool:
        for frame_match, inner, kind in CHECKBOX_PATTERNS:
            try:
                fl = page.frame_locator(frame_match)
                loc = fl.locator(inner) if inner else fl.locator(":root")
                if await loc.count() > 0:
                    el = loc.first
                    await el.scroll_into_view_if_needed()
                    box = await el.bounding_box()
                    if box:
                        # Humanize: glide the mouse to the widget, dwell, then click.
                        x = box["x"] + box["width"] / 2 + _jitter()
                        y = box["y"] + box["height"] / 2 + _jitter()
                        await page.mouse.move(x, y, steps=random.randint(15, 45))
                        await asyncio.sleep(_jitter(0.35, 1.1))
                    await el.click()
                    # Verify the widget accepted the click (checked state) within ~6s.
                    if await self._verified_checked(page, kind):
                        return True
            except Exception:
                continue
        return False

    async def _verified_checked(self, page, kind: str) -> bool:
        selector = CHECKED_SELECTORS.get(kind)
        if not selector:
            return True  # no introspectable state — assume accepted
        if kind == "turnstile":
            # Turnstile: the widget iframe grows a success check after passing.
            try:
                fl = page.frame_locator(selector)
                for _ in range(6):
                    if await fl.locator(":root").count() > 0:
                        html = await fl.locator("body").inner_html(timeout=1000)
                        if "check" in html.lower() or "success" in html.lower():
                            return True
                    await asyncio.sleep(1)
                return False
            except Exception:
                return False
        # recaptcha / hcaptcha: checked class flips inside the frame.
        for frame_match, inner, _k in CHECKBOX_PATTERNS:
            if _k != kind:
                continue
            try:
                fl = page.frame_locator(frame_match)
                for _ in range(6):
                    if await fl.locator(selector).count() > 0:
                        return True
                    await asyncio.sleep(1)
                return False
            except Exception:
                return False
        return False

    @staticmethod
    def _resolved(title: str) -> bool:
        t = title.strip().lower()
        return not (t.startswith("just a moment") or "verify you are human" in t or "attention required" in t)

    def _looks_flagged(self, page, body: str) -> bool:
        try:
            url = page.url
        except Exception:
            url = ""
        if any(m in url.lower() for m in FAIL_URL_MARKERS):
            return True
        # A pure interstitial shell is small and packed with challenge markers. A real
        # page that merely embeds a widget (login form, vendor demo) is NOT a block.
        if is_interstitial(body) and len(body) < 50_000:
            return True
        return False

    def _describe_failure(self, page, body: str) -> str:
        if not body:
            return "empty body (blocked)"
        return "flagged (challenge marker still present) or non-2xx response"

    async def _safe_body(self, page) -> str:
        try:
            return await page.content()
        except Exception:
            return ""

    async def _safe_cookies(self, page) -> list[dict]:
        try:
            raw = await page.context.cookies()
            out = []
            for c in raw:
                out.append({
                    "name": c.get("name"),
                    "value": c.get("value"),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                    "expires": c.get("expires", -1),
                    "secure": c.get("secure", False),
                    "httpOnly": c.get("httpOnly", False),
                    "sameSite": c.get("sameSite", "Lax"),
                })
            return out
        except Exception:
            return []

    async def _safe_status(self, page) -> int | None:
        try:
            resp = await page.evaluate("() => (performance.getEntriesByType('navigation')[0]?.responseStatus) || null")
            return int(resp) if resp else None
        except Exception:
            return None

    async def _safe_ua(self, page) -> str:
        try:
            return await page.evaluate("navigator.userAgent")
        except Exception:
            return ""

    async def _safe_title(self, page) -> str:
        try:
            return await page.title()
        except Exception:
            return ""

    async def _close_page(self, page) -> None:
        try:
            await page.close()
        except Exception:
            pass

    def _trace(self, session: BrowserSession, msg: str) -> None:
        import logging

        logging.getLogger("tokenbank").debug("[%s] %s", session.identity_id, msg)


class _UnbuiltSolver(Solver):
    """Placeholder for future backends (service/cv/human). Keeps the registry honest."""

    def __init__(self, name: str):
        self.name = name

    async def solve(self, req: SolveRequest, session: BrowserSession) -> SolveResult:
        return SolveResult(
            ok=False,
            error=f"solver backend '{self.name}' is not built — browser backend is the only one",
            mode=req.mode,
        )


_SOLVERS: dict[str, Solver] = {
    "browser": BrowserSolver(),
    "service": _UnbuiltSolver("service"),
    "cv": _UnbuiltSolver("cv"),
    "human": _UnbuiltSolver("human"),
}


def get_solver(name: str) -> Solver:
    return _SOLVERS.get(name, _SOLVERS["browser"])

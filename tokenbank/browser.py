"""Browser driver layer — owns the real-browser sessions the solvers drive.

Two engines:
  - patchright (default): Playwright fork with CDP-automation patches. Drives the
    installed Chrome via `channel=chrome` so the whole environment (TLS, HTTP/2, JS
    fingerprint) describes one real Chrome. Strongest default.
  - camoufox (optional): Firefox-based, randomized fingerprints. Selected via
    TOKENBANK_ENGINE=camoufox. Requires `python -m camoufox fetch` once.

A BrowserSession is one identity: either an ephemeral context (fresh per solve) or a
persistent profile dir (one profile per proxy/target, so cookies and history accumulate
naturally). An optional proxy routes the whole browser through an upstream exit so
solving happens from the exit IP.
"""

import time
import uuid
from dataclasses import dataclass

from .config import settings

CHALLENGE_MARKERS = (
    "Just a moment",
    "Verify you are human",
    "cf-browser-verification",
    "challenge-platform",
    "checking your browser",
    "Checking your browser",
    "enable javascript",
    "cf-chl",
    "__cf_chl",
    "turnstile",
    "perimeterx",
    "_px",
    "_abck",
    "verify you are",
)

# Signals that a page IS a pure challenge interstitial (a block), as opposed to a real
# page that merely EMBEDS a challenge widget (login forms, vendor demos). These are the
# fingerprints of the challenge shells themselves.
INTERSTITIAL_MARKERS = (
    "cf-browser-verification",
    "challenge-platform",
    "__cf_chl",
    "cf_chl_opt",
    "cf-chl-open",
    "Just a moment",
    "Verify you are human",
    "Checking your browser",
    "enable javascript and cookies",
    "sec_cpt",
    "_pxhd",
)


def has_challenge_marker(text: str) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in CHALLENGE_MARKERS)


def is_interstitial(text: str) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in INTERSTITIAL_MARKERS)


@dataclass
class SessionConfig:
    engine: str = settings.engine
    channel: str | None = settings.channel
    headless: bool = settings.headless
    mobile: bool = settings.mobile
    locale: str = settings.locale
    timezone: str = settings.timezone
    viewport: tuple[int, int] = settings.viewport_wh
    user_agent: str | None = settings.user_agent
    proxy: str | None = settings.proxy
    profile_dir: str | None = None  # persistent identity profile
    identity_id: str | None = None

    @property
    def is_mobile(self) -> bool:
        return self.mobile


class BrowserSession:
    """One real browser identity. `start()` brings the engine up; `new_page()` opens a tab."""

    def __init__(self, cfg: SessionConfig | None = None):
        self.cfg = cfg or SessionConfig()
        self.identity_id = self.cfg.identity_id or uuid.uuid4().hex[:12]
        self._pw = None
        self._browser = None
        self._context = None
        self.created_at = time.time()
        self.last_used = time.time()

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.cfg.engine == "patchright":
            await self._start_patchright()
        elif self.cfg.engine == "camoufox":
            await self._start_camoufox()
        else:
            raise RuntimeError(f"unknown engine '{self.cfg.engine}' (patchright|camoufox)")

    async def _start_patchright(self) -> None:
        try:
            from patchright.async_api import async_playwright
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "patchright not installed — run: pip install patchright"
            ) from e

        self._pw = await async_playwright().start()
        launch_opts: dict = {
            "headless": self.cfg.headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self.cfg.channel:
            launch_opts["channel"] = self.cfg.channel

        ctx_opts = self._context_options()
        if self.cfg.profile_dir:
            self._browser = await self._pw.chromium.launch_persistent_context(
                self.cfg.profile_dir, **launch_opts, **ctx_opts
            )
            self._context = self._browser
        else:
            self._browser = await self._pw.chromium.launch(**launch_opts)
            self._context = await self._browser.new_context(**ctx_opts)

    async def _start_camoufox(self) -> None:
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "camoufox not installed — run: pip install 'tokenbank[camoufox]' && python -m camoufox fetch"
            ) from e

        camo = AsyncCamoufox(
            headless=self.cfg.headless,
            proxy=self.cfg.proxy,
            os="linux",
            humanize=True,
        )
        self._browser = await camo.start()  # a BrowserContext
        self._context = self._browser

    def _context_options(self) -> dict:
        opts: dict = {
            "viewport": {"width": self.cfg.viewport[0], "height": self.cfg.viewport[1]},
            "locale": self.cfg.locale,
            "timezone_id": self.cfg.timezone,
            "color_scheme": "light",
        }
        # Mobile emulation: the mobile viewport + touch + a mobile UA describe one
        # coherent mobile browser (mirrors Emulation.setDeviceMetricsOverride + UA override).
        if self.cfg.is_mobile:
            opts["viewport"] = {"width": 390, "height": 844}  # iPhone 13-class
            opts["is_mobile"] = True
            opts["has_touch"] = True
            opts["device_scale_factor"] = 3
            if not self.cfg.user_agent or self.cfg.user_agent.startswith("Mozilla/5.0 (X11"):
                opts["user_agent"] = (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
                )
        if self.cfg.user_agent and not self.cfg.is_mobile:
            opts["user_agent"] = self.cfg.user_agent
        if self.cfg.proxy and self.cfg.engine == "patchright":
            # Playwright does NOT authenticate creds embedded in the server URL —
            # they must be separate fields, else the proxy answers 407.
            from urllib.parse import urlparse

            p = urlparse(self.cfg.proxy)
            proxy_opts: dict = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
            if p.username:
                proxy_opts["username"] = p.username
            if p.password:
                proxy_opts["password"] = p.password
            opts["proxy"] = proxy_opts
        return opts

    async def new_page(self):
        self.last_used = time.time()
        return await self._context.new_page()

    async def new_context(self):
        """A FRESH, clean context on the same browser — no cookies from other targets.

        Cross-target cookie contamination (pooling a session across different sites) is a
        correctness bug: minted tokens must only ever carry the target's own cookies.
        Persistent profiles are the alternative: one profile per proxy/target so cookies
        and history accumulate naturally.
        """
        if self.cfg.engine == "camoufox":
            raise RuntimeError("camoufox does not support per-solve contexts; use patchright")
        return await self._browser.new_context(**self._context_options())

    async def close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._pw = None

    # ── inspection helpers ─────────────────────────────────────────────────
    def engine(self) -> str:
        return self.cfg.engine

    def is_persistent(self) -> bool:
        return self.cfg.profile_dir is not None

    def age_s(self) -> float:
        return time.time() - self.created_at

    def idle_s(self) -> float:
        return time.time() - self.last_used

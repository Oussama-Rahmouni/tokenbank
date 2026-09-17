"""Solve farm — a managed pool of real-browser sessions, kept warm.

Two pieces on top of the pool:
  - PreSolver: background loop that mints tokens for configured targets ahead of
    demand, so the token bank is never cold.
  - AgingLoop: persistent identities browse benign pages periodically so their profiles
    accumulate real history.
"""

import asyncio
import logging
import random
import time
from pathlib import Path

from .config import Settings
from .solvers import SolveRequest, get_solver
from .tokenbank import TokenBank

log = logging.getLogger("tokenbank.farm")


class SessionPool:
    """Browser session pool.

    Two session kinds:
      - ephemeral (pool): fresh contexts per solve; keyed by proxy/engine.
      - persistent (profile): one BrowserSession per identity name, launched with a
        persistent user-data dir under identities/<profile> — one profile per
        proxy/target so cookies + history accumulate naturally.
    """

    def __init__(self, settings: Settings, identity_dir: Path):
        self._settings = settings
        self._identity_dir = identity_dir
        self._sessions: list = []
        self._profiles: dict[str, "BrowserSession"] = {}
        self._lock = asyncio.Lock()
        self._seq = 0

    async def acquire(
        self, proxy: str | None = None, engine: str | None = None, profile: str | None = None
    ) -> "BrowserSession":
        if profile:
            async with self._lock:
                if profile in self._profiles:
                    return self._profiles[profile]
            session = await self.create(proxy=proxy, engine=engine, profile=profile)
            async with self._lock:
                self._profiles[profile] = session
            return session

        # Ephemeral: prefer an idle session matching the request's proxy/engine.
        async with self._lock:
            for s in self._sessions:
                if s.cfg.proxy == (proxy or self._settings.proxy) and s.cfg.engine == (engine or self._settings.engine):
                    return s
        return await self.create(proxy=proxy, engine=engine)

    async def create(
        self, proxy: str | None = None, engine: str | None = None, profile: str | None = None
    ) -> "BrowserSession":
        from .browser import BrowserSession, SessionConfig

        self._seq += 1
        profile_dir = None
        identity_id = f"pool-{self._seq}"
        if profile:
            identity_id = profile
            profile_dir = str(self._identity_dir / _sanitise(profile))
        cfg = SessionConfig(
            engine=engine or self._settings.engine,
            channel=self._settings.channel,
            headless=self._settings.headless,
            locale=self._settings.locale,
            timezone=self._settings.timezone,
            viewport=self._settings.viewport_wh,
            user_agent=self._settings.user_agent,
            proxy=proxy or self._settings.proxy,
            identity_id=identity_id,
            profile_dir=profile_dir,
        )
        session = BrowserSession(cfg)
        log.info("starting session %s (%s)%s", session.identity_id, cfg.engine,
                 f" profile={profile}" if profile else "")
        await session.start()
        if not profile:
            async with self._lock:
                self._sessions.append(session)
                while len(self._sessions) > max(1, self._settings.pool_size):
                    oldest = min(self._sessions, key=lambda s: s.idle_s())
                    await oldest.close()
                    self._sessions.remove(oldest)
        return session

    async def release(self, session) -> None:
        pass

    async def retire(self, session) -> None:
        async with self._lock:
            for k, v in list(self._profiles.items()):
                if v is session:
                    del self._profiles[k]
            if session in self._sessions:
                self._sessions.remove(session)
        await session.close()

    async def persistent_sessions(self) -> list:
        async with self._lock:
            return list(self._profiles.values())

    async def stats(self) -> dict:
        return {
            "pool_size": len(self._sessions),
            "profiles": list(self._profiles.keys()),
            "engines": [s.engine() for s in self._sessions],
            "idle_s": [round(s.idle_s(), 1) for s in self._sessions],
        }

    async def close_all(self) -> None:
        async with self._lock:
            for s in self._sessions:
                await s.close()
            for s in self._profiles.values():
                await s.close()
            self._sessions.clear()
            self._profiles.clear()


def _sanitise(name: str) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9._-]", "_", name)


class PreSolver:
    """Background loop that mints tokens for configured targets ahead of demand."""

    def __init__(self, settings: Settings, pool: SessionPool, bank: TokenBank):
        self._settings = settings
        self._pool = pool
        self._bank = bank
        self._task: asyncio.Task | None = None
        self._runs = 0
        self._last_error: str | None = None

    def start(self) -> None:
        if not self._settings.presolve_targets:
            log.info("presolve disabled (TOKENBANK_PRESOLVE not set)")
            return
        self._task = asyncio.create_task(self._loop(), name="tokenbank-presolve")
        log.info("presolve started for %d target(s)", len(self._settings.presolve_targets))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self._top_up()
            except Exception as e:  # noqa: BLE001
                self._last_error = str(e)
                log.warning("presolve round failed: %s", e)
            await asyncio.sleep(self._settings.presolve_interval_s)

    async def _top_up(self) -> None:
        solver = get_solver("browser")
        for target in self._settings.presolve_targets:
            url = target.get("url")
            domain = target.get("domain") or _host_of(url)
            min_tokens = int(target.get("min_tokens", 3))
            if not url or not domain:
                continue
            have = await self._bank.count_available(domain)
            for _ in range(max(0, min_tokens - have)):
                session = await self._pool.acquire()
                try:
                    result = await solver.solve(SolveRequest(url=url, mode="tokens"), session)
                    if result.ok and result.cookies:
                        token_id = await self._bank.mint(
                            domain, result.cookies, headers={"user-agent": result.user_agent},
                            user_agent=result.user_agent,
                            ttl_s=self._settings.token_ttl_s,
                        )
                        self._runs += 1
                        log.info("presolved %s → token %s (%d cookies)", domain, token_id, len(result.cookies))
                    elif result.flagged:
                        await self._pool.retire(session)
                finally:
                    pass

    async def _bank_available(self, domain: str) -> int:
        return await self._bank.count_available(domain)

    def stats(self) -> dict:
        return {"runs": self._runs, "last_error": self._last_error, "targets": self._settings.presolve_targets}


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or ""
    except Exception:
        return ""


class AgingLoop:
    """Natural-aging loop for persistent identity profiles.

    A profile with cookies but no browsing history is a tell. Every `interval_s`, each
    persistent session visits a small set of benign pages so the identity accumulates
    real history and looks like a person who left a browser open.
    Env: TOKENBANK_AGING_INTERVAL (s), TOKENBANK_AGING_URLS (comma-separated).
    """

    def __init__(self, settings: Settings, pool: SessionPool):
        self._settings = settings
        self._pool = pool
        self._task: asyncio.Task | None = None
        self._runs = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="tokenbank-aging")
        log.info("aging loop started (interval=%ss)", self._settings.aging_interval_s)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self._age_once()
            except Exception as e:  # noqa: BLE001
                log.warning("aging round failed: %s", e)
            await asyncio.sleep(self._settings.aging_interval_s)

    async def _age_once(self) -> None:
        urls = self._settings.aging_urls
        if not urls:
            return
        sessions = await self._pool.persistent_sessions()
        for s in sessions:
            try:
                page = await s.new_page()
                for u in urls[:3]:
                    try:
                        await page.goto(u, wait_until="domcontentloaded", timeout=20000)
                        await asyncio.sleep(1.0 + random.uniform(0, 1.5))
                    except Exception:
                        pass
                await page.close()
                self._runs += 1
                log.info("aged identity %s (%d visits)", s.identity_id, len(urls[:3]))
            except Exception as e:  # noqa: BLE001
                log.warning("aging failed for %s: %s", s.identity_id, e)

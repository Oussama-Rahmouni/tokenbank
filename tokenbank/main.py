"""tokenbank HTTP service — real-browser challenge solver + warm token bank.

  tokenbank-server          # or: uvicorn tokenbank.main:app --host 127.0.0.1 --port 8123 --loop asyncio

Endpoints:
  GET  /v1/health           liveness + pool/bank stats
  POST /v1/solve            solve a challenge (mode=page | mode=tokens)
  GET  /v1/token/{domain}   rent a pre-solved token from the bank
  POST /v1/token/{id}/release
"""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import ensure_state_dirs, settings
from .farm import AgingLoop, PreSolver, SessionPool
from .solvers import SolveRequest, SolveResult, get_solver
from .tokenbank import TokenBank

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tokenbank")

state = {"pool": None, "bank": None, "presolver": None, "aging": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_state_dirs(settings)
    bank = TokenBank(settings.state_dir / "tokens" / "bank.db")
    await bank.init()
    pool = SessionPool(settings, settings.state_dir / "identities")
    presolver = PreSolver(settings, pool, bank)
    aging = AgingLoop(settings, pool)
    state["bank"] = bank
    state["pool"] = pool
    state["presolver"] = presolver
    state["aging"] = aging
    presolver.start()
    aging.start()
    log.info("tokenbank up on %s:%s (engine=%s, pool=%d)", settings.host, settings.port, settings.engine, settings.pool_size)
    yield
    presolver.stop()
    aging.stop()
    await pool.close_all()
    await bank.close()


app = FastAPI(title="tokenbank", version="0.1.0", lifespan=lifespan)


class SolvePayload(BaseModel):
    url: str
    mode: str = Field(default="page", pattern="^(page|tokens)$")
    cookies: list[dict] = Field(default_factory=list)
    headers: dict = Field(default_factory=dict)
    proxy: str | None = None
    engine: str | None = None
    solver: str = Field(default="browser")
    timeout_s: int | None = None
    domain: str | None = None  # if set, mint a token into the bank (mode=tokens)
    profile: str | None = None  # persistent identity profile (one profile = one proxy/target)


@app.get("/v1/health")
async def health():
    pool_stats = await state["pool"].stats() if state["pool"] else {}
    bank_stats = await state["bank"].stats() if state["bank"] else {}
    presolve = state["presolver"].stats() if state["presolver"] else {}
    aging_runs = state["aging"]._runs if state["aging"] else 0  # noqa: SLF001
    return {
        "ok": True,
        "engine": settings.engine,
        "pool": pool_stats,
        "bank": bank_stats,
        "presolve": presolve,
        "aging_runs": aging_runs,
    }


@app.post("/v1/solve", response_model=dict)
async def solve(payload: SolvePayload):
    pool = state["pool"]
    if pool is None:
        raise HTTPException(status_code=503, detail="tokenbank not initialised")

    solver = get_solver(payload.solver)
    session = await pool.acquire(proxy=payload.proxy, engine=payload.engine, profile=payload.profile)
    result: SolveResult = await solver.solve(
        SolveRequest(
            url=payload.url,
            mode=payload.mode,
            cookies=payload.cookies,
            headers=payload.headers,
            proxy=payload.proxy,
            engine=payload.engine,
            timeout_s=payload.timeout_s,
        ),
        session,
    )

    # Burn-on-flag: an identity that got downgraded to an image grid leaves the pool.
    if result.flagged:
        await pool.retire(session)

    if result.ok and result.cookies:
        domain = payload.domain or _host_of(payload.url)
        # Only the target's own cookies — never a pooled session's foreign cookies.
        cookies = _cookies_for_domain(result.cookies, domain)
        token_id = await state["bank"].mint(
            domain,
            cookies,
            headers={"user-agent": result.user_agent},
            user_agent=result.user_agent,
            proxy_key=payload.proxy or settings.proxy or "",
            ttl_s=settings.token_ttl_s,
        )
        result.token_id = token_id

    return _result_dict(result)


@app.get("/v1/token/{domain}")
async def acquire_token(domain: str, proxy: str | None = None):
    token = await state["bank"].acquire(domain, proxy_key=proxy or settings.proxy or None)
    if token is None:
        raise HTTPException(status_code=404, detail=f"no warm token for {domain}")
    return {
        "token_id": token.token_id,
        "domain": token.domain,
        "cookies": token.cookies,
        "headers": token.headers,
        "user_agent": token.user_agent,
        "expires_at": token.expires_at,
    }


@app.post("/v1/token/{token_id}/release")
async def release_token(token_id: str):
    ok = await state["bank"].release(token_id)
    return {"released": ok, "token_id": token_id}


@app.post("/v1/token/sweep")
async def sweep_tokens():
    n = await state["bank"].sweep()
    return {"expired": n}


def _result_dict(r: SolveResult) -> dict:
    return {
        "ok": r.ok,
        "url": r.url,
        "status": r.status,
        "headers": r.headers,
        "body": r.body,
        "cookies": r.cookies,
        "user_agent": r.user_agent,
        "token_id": r.token_id,
        "flagged": r.flagged,
        "error": r.error,
        "duration_ms": r.duration_ms,
        "mode": r.mode,
    }


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _base_domain(domain: str) -> str:
    """Strip www. and subdomains → registrable-ish base for cookie filtering."""
    parts = domain.split(".")
    if len(parts) > 2:
        return ".".join(parts[-2:])
    return domain


def _cookies_for_domain(cookies: list[dict], domain: str) -> list[dict]:
    """Only cookies that belong to the target's base domain (or subdomains of it)."""
    base = _base_domain(domain).lower()
    out = []
    for c in cookies:
        cdomain = (c.get("domain") or "").lower().lstrip(".")
        if cdomain == base or cdomain.endswith("." + base):
            out.append(c)
    return out


def run() -> None:
    """Console entrypoint. Forces the asyncio event loop: uvicorn[standard] defaults to
    uvloop, which deadlocks playwright/patchright's greenlet bridge (solves hang
    forever). Plain asyncio works."""
    import uvicorn

    uvicorn.run(
        "tokenbank.main:app",
        host=settings.host,
        port=settings.port,
        loop="asyncio",
    )


if __name__ == "__main__":
    run()

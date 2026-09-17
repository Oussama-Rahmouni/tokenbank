"""Token Bank — warm pool of solved-challenge cookies, minted ahead of demand and
rented per request. Turns each interactive/invisible solve into an amortized cost
instead of a per-request cost.

Tokens carry the proxy/exit key they were minted on so rentals stay IP-sticky: a token
is only valid from the identity (IP + user agent + TLS profile) that solved it.
"""

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
  token_id TEXT PRIMARY KEY,
  domain   TEXT NOT NULL,
  kind     TEXT NOT NULL DEFAULT 'challenge',
  cookies  TEXT NOT NULL,
  headers  TEXT NOT NULL DEFAULT '{}',
  user_agent TEXT DEFAULT '',
  proxy_key TEXT DEFAULT '',
  minted_at   INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  last_rented_at INTEGER DEFAULT 0,
  rentals     INTEGER DEFAULT 0,
  status      TEXT NOT NULL DEFAULT 'available'   -- available | rented | expired
);
CREATE INDEX IF NOT EXISTS idx_tokens_domain ON tokens(domain, status, expires_at);
"""


@dataclass
class Token:
    token_id: str
    domain: str
    kind: str
    cookies: list[dict]
    headers: dict
    user_agent: str
    proxy_key: str
    minted_at: int
    expires_at: int


class TokenBank:
    def __init__(self, db_path: Path):
        self._db_path = str(db_path)

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if hasattr(self, "_db"):
            await self._db.close()

    async def mint(
        self,
        domain: str,
        cookies: list[dict],
        headers: dict | None = None,
        user_agent: str = "",
        proxy_key: str = "",
        kind: str = "challenge",
        ttl_s: int = 3600,
    ) -> str:
        token_id = uuid.uuid4().hex[:16]
        now = int(time.time())
        await self._db.execute(
            "INSERT INTO tokens (token_id, domain, kind, cookies, headers, user_agent,"
            " proxy_key, minted_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                token_id,
                domain,
                kind,
                _dumps(cookies),
                _dumps(headers or {}),
                user_agent,
                proxy_key,
                now,
                now + ttl_s,
            ),
        )
        await self._db.commit()
        return token_id

    async def acquire(self, domain: str, proxy_key: str | None = None) -> Token | None:
        now = int(time.time())
        cur = await self._db.execute(
            "SELECT token_id, domain, kind, cookies, headers, user_agent, proxy_key,"
            " minted_at, expires_at FROM tokens"
            " WHERE domain=? AND status='available' AND expires_at>?"
            + (" AND proxy_key=?" if proxy_key else "")
            + " ORDER BY expires_at ASC LIMIT 1",
            (domain, now, proxy_key) if proxy_key else (domain, now),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        await self._db.execute(
            "UPDATE tokens SET status='rented', last_rented_at=?, rentals=rentals+1 WHERE token_id=?",
            (now, row[0]),
        )
        await self._db.commit()
        return _row_to_token(row)

    async def count_available(self, domain: str) -> int:
        now = int(time.time())
        cur = await self._db.execute(
            "SELECT COUNT(*) FROM tokens WHERE domain=? AND status='available' AND expires_at>?",
            (domain, now),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def release(self, token_id: str) -> bool:
        now = int(time.time())
        cur = await self._db.execute(
            "UPDATE tokens SET status='available' WHERE token_id=? AND expires_at>?",
            (token_id, now),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def sweep(self) -> int:
        now = int(time.time())
        cur = await self._db.execute("UPDATE tokens SET status='expired' WHERE expires_at<=?", (now,))
        await self._db.commit()
        return cur.rowcount or 0

    async def stats(self) -> dict:
        rows = await self._db.execute_fetchall(
            "SELECT status, COUNT(*) FROM tokens GROUP BY status"
        )
        return {k: v for k, v in rows}


def _dumps(obj) -> str:
    import json

    return json.dumps(obj, separators=(",", ":"))


def _row_to_token(row) -> Token:
    import json

    return Token(
        token_id=row[0],
        domain=row[1],
        kind=row[2],
        cookies=json.loads(row[3]),
        headers=json.loads(row[4]),
        user_agent=row[5],
        proxy_key=row[6],
        minted_at=row[7],
        expires_at=row[8],
    )

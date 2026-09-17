"""Runtime configuration — env-driven, no config files."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _state_dir() -> Path:
    return Path(os.environ.get("TOKENBANK_STATE_DIR", Path.home() / ".tokenbank"))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Real Chrome desktop UA — never the leaky "HeadlessChrome" string. Coherent with the
# channel=chrome build we drive; override per-target with TOKENBANK_USER_AGENT.
_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


@dataclass
class Settings:
    host: str = field(default_factory=lambda: os.environ.get("TOKENBANK_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("TOKENBANK_PORT", 8123))

    state_dir: Path = field(default_factory=_state_dir)

    # Browser engine
    engine: str = field(default_factory=lambda: os.environ.get("TOKENBANK_ENGINE", "patchright"))
    channel: str | None = field(default_factory=lambda: os.environ.get("TOKENBANK_CHANNEL", "chrome") or None)
    headless: bool = field(default_factory=lambda: os.environ.get("TOKENBANK_HEADLESS", "1") != "0")

    # Stealth extras:
    #   TOKENBANK_CHANNEL="" → unbranded/patchright-bundled chromium (stealthier on some sites)
    #   TOKENBANK_MOBILE=1   → mobile emulation (viewport + touch + mobile UA, one coherent device)
    mobile: bool = field(default_factory=lambda: os.environ.get("TOKENBANK_MOBILE", "0") == "1")

    # Identity
    locale: str = field(default_factory=lambda: os.environ.get("TOKENBANK_LOCALE", "en-US"))
    timezone: str = field(default_factory=lambda: os.environ.get("TOKENBANK_TIMEZONE", "America/New_York"))
    viewport: str = field(default_factory=lambda: os.environ.get("TOKENBANK_VIEWPORT", "1366x850"))
    user_agent: str | None = field(default_factory=lambda: os.environ.get("TOKENBANK_USER_AGENT") or _DEFAULT_UA)

    # Routing — an upstream proxy (http:// or socks5://) to solve + fetch through
    proxy: str | None = field(default_factory=lambda: os.environ.get("TOKENBANK_PROXY") or None)

    # Farm / solver
    pool_size: int = field(default_factory=lambda: _env_int("TOKENBANK_POOL_SIZE", 2))
    solve_timeout_s: int = field(default_factory=lambda: _env_int("TOKENBANK_SOLVE_TIMEOUT", 75))
    max_body_bytes: int = field(default_factory=lambda: _env_int("TOKENBANK_MAX_BODY", 2_000_000))

    # Warm pool of tokens, pre-solved ahead of demand.
    # JSON: [{"url": "...", "domain": "...", "min_tokens": 3}]
    presolve_targets: list[dict] = field(default_factory=lambda: _load_presolve())
    presolve_interval_s: int = field(default_factory=lambda: _env_int("TOKENBANK_PRESOLVE_INTERVAL", 600))

    # Token bank
    token_ttl_s: int = field(default_factory=lambda: _env_int("TOKENBANK_TOKEN_TTL", 3600))

    # Natural aging: persistent identities browse benign pages periodically so their
    # profiles accumulate real history instead of sitting idle.
    aging_interval_s: int = field(default_factory=lambda: _env_int("TOKENBANK_AGING_INTERVAL", 600))
    aging_urls: list[str] = field(
        default_factory=lambda: [u for u in os.environ.get("TOKENBANK_AGING_URLS", "").split(",") if u]
    )

    @property
    def viewport_wh(self) -> tuple[int, int]:
        try:
            w, h = self.viewport.lower().split("x")
            return int(w), int(h)
        except (ValueError, AttributeError):
            return 1366, 850


def _load_presolve() -> list[dict]:
    raw = os.environ.get("TOKENBANK_PRESOLVE")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def ensure_state_dirs(settings: Settings) -> None:
    for sub in ("identities", "tokens"):
        (settings.state_dir / sub).mkdir(parents=True, exist_ok=True)


settings = Settings()

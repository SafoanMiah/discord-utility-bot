"""Environment configuration and constants for Iris."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Real env vars take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv(REPO_ROOT / ".env")

# DISCORD_BOT_TOKEN accepted as a fallback for compatibility with older setups.
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", str(REPO_ROOT / "iris.db"))
GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None

HEARTBEAT_SECONDS = 60

# circlebot.xyz — its voice join/leave log embeds feed /backlog vc.
CIRCLEBOT_ID = 497196352866877441

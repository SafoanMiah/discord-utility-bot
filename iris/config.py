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
# The channel for administrative messages (daily DB backups, alerts) is no
# longer an env var — it's set at runtime with /admin set and lives in the db.

HEARTBEAT_SECONDS = 60

# /unmute: how long one shield lasts, how long before a member can raise
# another (admins skip this), and a circuit breaker so a mute war can't turn
# into an endless stream of edits at Discord.
UNMUTE_SHIELD_SECONDS = 600
UNMUTE_COOLDOWN_SECONDS = 86_400
UNMUTE_MAX_UNDOS = 25

# circlebot.xyz — its voice join/leave log embeds feed /backlog vc.
CIRCLEBOT_ID = 497196352866877441

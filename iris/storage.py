"""SQLite repository. The ONLY module that talks to the database.

Swapping SQLite for Postgres later means reimplementing this module and
nothing else. All timestamps in and out are Unix epoch seconds, UTC.
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Storage:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._migrate()
        await self._db.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        await self._db.commit()

    async def _migrate(self) -> None:
        """Bring pre-existing databases up to the current schema before the
        schema script runs (its index DDL assumes the new columns exist)."""

        async def columns(table: str) -> list[str]:
            async with self._db.execute(f"PRAGMA table_info({table})") as cur:
                return [row[1] for row in await cur.fetchall()]

        msg_cols = await columns("messages")
        if msg_cols and "message_id" not in msg_cols:
            await self._db.execute("ALTER TABLE messages ADD COLUMN message_id INTEGER")
        vc_cols = await columns("voice_sessions")
        if vc_cols and "source" not in vc_cols:
            await self._db.execute(
                "ALTER TABLE voice_sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'live'"
            )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Storage.open() not called"
        return self._db

    # -- timezones ----------------------------------------------------------

    async def set_timezone(self, user_id: int, tz: str) -> None:
        await self.db.execute(
            "INSERT INTO users (user_id, tz) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET tz = excluded.tz",
            (user_id, tz),
        )
        await self.db.commit()

    async def get_timezone(self, user_id: int) -> str | None:
        async with self.db.execute(
            "SELECT tz FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def clear_timezone(self, user_id: int) -> None:
        await self.db.execute("UPDATE users SET tz = NULL WHERE user_id = ?", (user_id,))
        await self.db.commit()

    # -- capture ------------------------------------------------------------

    async def log_message(
        self,
        user_id: int,
        guild_id: int,
        channel_id: int,
        ts_utc: int,
        message_id: int | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO messages (user_id, guild_id, channel_id, ts_utc, message_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, guild_id, channel_id, ts_utc, message_id),
        )
        await self.db.commit()

    async def log_messages_bulk(
        self, rows: list[tuple[int, int, int, int, int]]
    ) -> int:
        """Insert (message_id, user_id, guild_id, channel_id, ts_utc) rows,
        silently skipping already-recorded message ids. Returns rows inserted."""
        if not rows:
            return 0
        cur = await self.db.executemany(
            "INSERT OR IGNORE INTO messages (message_id, user_id, guild_id, channel_id, ts_utc)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await self.db.commit()
        return cur.rowcount

    async def purge_legacy_messages(self, guild_id: int, channel_id: int) -> None:
        """Delete live-captured rows that predate message-id tracking for one
        channel. Called only after a full backfill pass re-recorded that
        channel's history with ids, so nothing is actually lost."""
        await self.db.execute(
            "DELETE FROM messages WHERE guild_id = ? AND channel_id = ? AND message_id IS NULL",
            (guild_id, channel_id),
        )
        await self.db.commit()

    async def open_voice_session(
        self, user_id: int, guild_id: int, channel_id: int, start_utc: int
    ) -> None:
        # Defensive: a missed leave event would otherwise leave two open
        # sessions for one user. Close any stale one at its last heartbeat.
        await self.db.execute(
            "UPDATE voice_sessions"
            " SET end_utc = COALESCE(last_heartbeat_utc, start_utc)"
            " WHERE user_id = ? AND guild_id = ? AND end_utc IS NULL",
            (user_id, guild_id),
        )
        await self.db.execute(
            "INSERT INTO voice_sessions (user_id, guild_id, channel_id, start_utc)"
            " VALUES (?, ?, ?, ?)",
            (user_id, guild_id, channel_id, start_utc),
        )
        await self.db.commit()

    async def close_voice_session(self, user_id: int, guild_id: int, end_utc: int) -> None:
        await self.db.execute(
            "UPDATE voice_sessions SET end_utc = ?"
            " WHERE user_id = ? AND guild_id = ? AND end_utc IS NULL",
            (end_utc, user_id, guild_id),
        )
        await self.db.commit()

    async def heartbeat(self, active_user_ids: list[int], ts: int) -> None:
        if not active_user_ids:
            return
        placeholders = ",".join("?" * len(active_user_ids))
        await self.db.execute(
            f"UPDATE voice_sessions SET last_heartbeat_utc = ?"
            f" WHERE end_utc IS NULL AND user_id IN ({placeholders})",
            (ts, *active_user_ids),
        )
        await self.db.commit()

    async def reconcile_open_sessions(self, now: int) -> int:
        """Close sessions left open by a previous run at their last heartbeat
        (start time if they never beat). Returns the number closed."""
        cur = await self.db.execute(
            "UPDATE voice_sessions"
            " SET end_utc = MIN(COALESCE(last_heartbeat_utc, start_utc), ?)"
            " WHERE end_utc IS NULL",
            (now,),
        )
        await self.db.commit()
        return cur.rowcount

    async def close_all_open_sessions(self, now: int) -> None:
        """Graceful shutdown: everyone in VC right now leaves at `now`."""
        await self.db.execute(
            "UPDATE voice_sessions SET end_utc = ? WHERE end_utc IS NULL", (now,)
        )
        await self.db.commit()

    async def add_voice_sessions_bulk(
        self,
        guild_id: int,
        rows: list[tuple[int, int, int, int]],
        source: str = "backlog",
    ) -> int:
        """Insert already-closed (user_id, channel_id, start_utc, end_utc)
        sessions, tagged with their source. Returns rows inserted."""
        if not rows:
            return 0
        cur = await self.db.executemany(
            "INSERT INTO voice_sessions (user_id, guild_id, channel_id, start_utc, end_utc, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(u, guild_id, c, s, e, source) for u, c, s, e in rows],
        )
        await self.db.commit()
        return cur.rowcount

    async def delete_voice_sessions_by_source(self, guild_id: int, source: str) -> int:
        """Remove a previous import wholesale, so re-running it can't duplicate."""
        cur = await self.db.execute(
            "DELETE FROM voice_sessions WHERE guild_id = ? AND source = ?",
            (guild_id, source),
        )
        await self.db.commit()
        return cur.rowcount

    async def earliest_live_voice_start(self, guild_id: int) -> int | None:
        """When Iris's own voice capture began — imports are capped here so
        they can never overlap live-recorded sessions."""
        async with self.db.execute(
            "SELECT MIN(start_utc) FROM voice_sessions WHERE guild_id = ? AND source = 'live'",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def get_open_sessions(self) -> list[tuple[int, int, int, int]]:
        """Rows of (user_id, guild_id, channel_id, start_utc) still open."""
        async with self.db.execute(
            "SELECT user_id, guild_id, channel_id, start_utc"
            " FROM voice_sessions WHERE end_utc IS NULL"
        ) as cur:
            return await cur.fetchall()

    # -- reads --------------------------------------------------------------

    async def get_messages(
        self, user_id: int, guild_id: int, since: int | None = None
    ) -> list[tuple[int, int]]:
        """Rows of (channel_id, ts_utc)."""
        sql = "SELECT channel_id, ts_utc FROM messages WHERE user_id = ? AND guild_id = ?"
        params: list[int] = [user_id, guild_id]
        if since is not None:
            sql += " AND ts_utc >= ?"
            params.append(since)
        async with self.db.execute(sql + " ORDER BY ts_utc", params) as cur:
            return await cur.fetchall()

    async def get_voice_sessions(
        self, user_id: int, guild_id: int, since: int | None = None
    ) -> list[tuple[int, int, int]]:
        """Rows of (channel_id, start_utc, end_utc). Closed sessions only."""
        sql = (
            "SELECT channel_id, start_utc, end_utc FROM voice_sessions"
            " WHERE user_id = ? AND guild_id = ? AND end_utc IS NOT NULL"
        )
        params: list[int] = [user_id, guild_id]
        if since is not None:
            sql += " AND end_utc >= ?"
            params.append(since)
        async with self.db.execute(sql + " ORDER BY start_utc", params) as cur:
            return await cur.fetchall()

    # -- privacy ------------------------------------------------------------

    async def set_optout(self, user_id: int) -> None:
        """Set the opt-out flag AND purge the user's recorded history."""
        await self.db.execute(
            "INSERT INTO users (user_id, opted_out) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET opted_out = 1",
            (user_id,),
        )
        await self.db.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        await self.db.execute("DELETE FROM voice_sessions WHERE user_id = ?", (user_id,))
        await self.db.commit()

    async def set_optin(self, user_id: int) -> None:
        """Clear the flag. Does not (cannot) restore purged data."""
        await self.db.execute(
            "INSERT INTO users (user_id, opted_out) VALUES (?, 0) "
            "ON CONFLICT(user_id) DO UPDATE SET opted_out = 0",
            (user_id,),
        )
        await self.db.commit()

    async def is_opted_out(self, user_id: int) -> bool:
        async with self.db.execute(
            "SELECT opted_out FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return bool(row and row[0])

    async def get_opted_out_ids(self) -> set[int]:
        """All opted-out user ids, for the in-memory capture filter."""
        async with self.db.execute("SELECT user_id FROM users WHERE opted_out = 1") as cur:
            return {row[0] for row in await cur.fetchall()}

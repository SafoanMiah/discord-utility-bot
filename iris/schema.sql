-- Iris schema. All timestamps are Unix epoch seconds, UTC.

CREATE TABLE IF NOT EXISTS users (
  user_id     INTEGER PRIMARY KEY,
  tz          TEXT,              -- IANA name e.g. 'Europe/London'; NULL = unset (UTC)
  opted_out   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL,
  guild_id    INTEGER NOT NULL,
  channel_id  INTEGER NOT NULL,  -- stored now for future per-channel views
  ts_utc      INTEGER NOT NULL,
  message_id  INTEGER            -- Discord snowflake; dedupes /backlog re-runs
);

CREATE TABLE IF NOT EXISTS voice_sessions (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id            INTEGER NOT NULL,
  guild_id           INTEGER NOT NULL,
  channel_id         INTEGER NOT NULL,
  start_utc          INTEGER NOT NULL,
  end_utc            INTEGER,          -- NULL = open (in progress)
  last_heartbeat_utc INTEGER,          -- updated while open; used for crash recovery
  source             TEXT NOT NULL DEFAULT 'live'  -- 'live' or 'backlog' (/backlog vc)
);

CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, guild_id, ts_utc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_mid ON messages(message_id)
  WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_voice_user    ON voice_sessions(user_id, guild_id, start_utc);
CREATE INDEX IF NOT EXISTS idx_voice_open    ON voice_sessions(end_utc);

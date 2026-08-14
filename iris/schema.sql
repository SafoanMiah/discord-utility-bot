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

-- One row per stretch a member played a given game (Discord Rich Presence).
-- Same lifecycle as voice_sessions: open on start, closed on stop, heartbeat
-- while open for crash recovery. Only the game NAME is stored, never details.
CREATE TABLE IF NOT EXISTS game_sessions (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id            INTEGER NOT NULL,
  guild_id           INTEGER NOT NULL,
  game               TEXT NOT NULL,    -- activity name, e.g. 'VALORANT'
  start_utc          INTEGER NOT NULL,
  end_utc            INTEGER,          -- NULL = open (in progress)
  last_heartbeat_utc INTEGER           -- updated while open; used for crash recovery
);

-- Bot-wide settings as key/value pairs, e.g. 'admin_channel_id'. Not
-- guild-scoped: the backup this feeds contains every guild's data.
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- /vote polls. Buttons on the posted message drive everything; the message id
-- links a live message back to its row so the persistent view can be rebuilt
-- after a restart. Nothing here is user message content.
CREATE TABLE IF NOT EXISTS votes (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id     INTEGER NOT NULL,
  channel_id   INTEGER NOT NULL,
  message_id   INTEGER,                       -- set once the message is posted
  creator_id   INTEGER NOT NULL,
  title        TEXT NOT NULL,
  anonymous    INTEGER NOT NULL DEFAULT 0,    -- 1 = hide who voted, show counts only
  multiple     INTEGER NOT NULL DEFAULT 0,    -- 1 = pick many, 0 = single choice
  closed       INTEGER NOT NULL DEFAULT 0,
  created_utc  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS vote_options (
  vote_id  INTEGER NOT NULL,
  idx      INTEGER NOT NULL,                  -- 0-based position / button order
  label    TEXT NOT NULL,
  dm       TEXT,                              -- optional ephemeral note shown to the voter on selection
  role_id  INTEGER,                           -- optional role granted while this option is selected
  PRIMARY KEY (vote_id, idx)
);

CREATE TABLE IF NOT EXISTS vote_ballots (
  vote_id  INTEGER NOT NULL,
  user_id  INTEGER NOT NULL,
  idx      INTEGER NOT NULL,
  PRIMARY KEY (vote_id, user_id, idx)         -- single-choice enforced in code
);

-- /unmute shields. While a row is live, Iris reverses any server mute or
-- deafen landing on that member. Rows are dropped once they expire; the
-- in-memory copy in bot.py is what the voice event actually checks, so this
-- table exists purely so shields survive a restart.
CREATE TABLE IF NOT EXISTS unmute_shields (
  guild_id     INTEGER NOT NULL,
  user_id      INTEGER NOT NULL,
  granted_by   INTEGER NOT NULL,
  granted_utc  INTEGER NOT NULL,
  expires_utc  INTEGER NOT NULL,
  PRIMARY KEY (guild_id, user_id)
);

-- When each member last spent a /unmute, for the once-a-day limit.
CREATE TABLE IF NOT EXISTS unmute_uses (
  guild_id  INTEGER NOT NULL,
  user_id   INTEGER NOT NULL,
  last_utc  INTEGER NOT NULL,
  PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, guild_id, ts_utc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_mid ON messages(message_id)
  WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_voice_user    ON voice_sessions(user_id, guild_id, start_utc);
CREATE INDEX IF NOT EXISTS idx_voice_open    ON voice_sessions(end_utc);
CREATE INDEX IF NOT EXISTS idx_game_user     ON game_sessions(user_id, guild_id, start_utc);
CREATE INDEX IF NOT EXISTS idx_game_open     ON game_sessions(end_utc);
CREATE INDEX IF NOT EXISTS idx_vote_ballots  ON vote_ballots(vote_id);
CREATE INDEX IF NOT EXISTS idx_votes_open    ON votes(closed);

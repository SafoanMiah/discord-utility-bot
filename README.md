# Iris — Discord activity & timekeeping bot

Iris logs per-user chat and voice-channel activity for one private server,
stores timezones, and renders activity/stat views as styled PNG images.
No gamification, no message content stored — metadata only.

## Layout

```
iris/
  bot.py         # entry point: client, intents, event handlers, slash commands
  storage.py     # repository: the ONLY module that talks to the DB (SQLite via aiosqlite)
  analysis.py    # pure functions: rows + tz -> bucketed data. No I/O.
  charts.py      # matplotlib rendering -> PNG bytes
  theme.py       # rcParams, palette, styled figure factory
  config.py      # env vars, constants
  schema.sql     # table + index DDL
  fonts/         # bundled Inter .ttf (OFL licence included)
tests/           # unit tests for analysis + storage
preview.py       # renders sample PNGs with fake data (visual iteration, no Discord needed)
```

Nothing downstream touches storage directly — swapping SQLite for Postgres
later means reimplementing `storage.py` only.

## Setup

```
python -m venv .venv
.venv/bin/pip install -r iris/requirements.txt
```

Config via env vars (a `.env` file in the repo root is also read):

| Var | Meaning |
|---|---|
| `DISCORD_TOKEN` | bot token (`DISCORD_BOT_TOKEN` also accepted) |
| `DB_PATH` | SQLite file path (default `./iris.db`) |
| `GUILD_ID` | optional — register commands to one guild for instant sync during dev |

In the Discord Developer Portal enable **both** privileged intents
(Bot → Privileged Gateway Intents): **Server Members** (join dates, voice
snapshots) and **Message Content**. The latter exists for exactly one reason:
Discord hides other bots' *embeds* without it, and `/backlog vc` parses
CircleBot's log embeds. Iris never reads or stores anyone's message text.

Run: `python -m iris.bot`

## Commands

- `/timezone set <zone>` · `/timezone show` · `/timezone clear` — IANA zone with
  autocomplete; ephemeral text replies. (The spec's bare `/timezone` became
  `/timezone show` — Discord can't invoke a command group with no subcommand.)
- `/activity <user> [day]` — composite PNG: messages/hour, voice minutes/hour,
  and day-of-week mini-panels; with `day`, both hour panels filtered to that
  weekday. All times in the **requester's** timezone.
- `/stats <user>` — stats card PNG: totals, most active hour/day, voice-per-message,
  session stats, tracked-since vs joined-server, last active, active days.
- `/privacy optout` — stop logging **and delete** recorded history. `/privacy optin`
  resumes logging (deleted data is not restored).
- `/backlog chats` (server managers only) — scans every text channel's full
  history and logs past message activity. Safe to re-run: rows are deduplicated
  by Discord message id. Bots, opted-out members, and system messages (joins,
  boosts, pins) are excluded, exactly as in live capture. Threads/forum posts
  aren't scanned.
- `/backlog vc <channel>` (server managers only) — voice has no history API,
  but if CircleBot (circlebot.xyz) logs to a channel, this reads its
  join/leave embeds and rebuilds past sessions from them. Joins pair with
  leaves per user (a join while one is open counts as a channel move);
  unpaired events from log gaps are dropped rather than guessed. Imports are
  tagged `source='backlog'`, so re-running replaces the previous import, and
  everything is capped at the moment Iris's own live voice capture began —
  nothing is ever counted twice. Requires the Message Content intent (see
  setup) to read the embeds.

Notes on semantics:
- "Voice per message" is total VC time ÷ total messages.
- "Most active hour/day" combines chat and voice, each normalised to its own
  total so heavy chatters and heavy VC users weigh equally.
- "Tracked since" is the earliest logged event (Iris only knows from when it
  started logging); "Joined server" is the real Discord join date.

## Reliability model

- Voice time is stored as raw sessions (start/end epoch UTC), never
  pre-bucketed — local-hour bucketing happens at read time after timezone
  conversion, so DST and fractional offsets can't misassign minutes.
- A 60 s heartbeat stamps open sessions; on startup, sessions left open by a
  crash are closed at their last heartbeat (≤ 60 s data loss), then members
  currently in voice are re-snapshotted.
- On SIGTERM (systemd stop/restart) all open sessions are closed at "now".

## Deployment (Oracle Always Free ARM VM or similar)

matplotlib wants real RAM — don't use 128 MB free panels. Example systemd unit:

```ini
[Unit]
Description=Iris Discord bot
After=network-online.target

[Service]
User=iris
WorkingDirectory=/opt/iris
ExecStart=/opt/iris/.venv/bin/python -m iris.bot
Restart=on-failure
RestartSec=5
Environment=DISCORD_TOKEN=...
Environment=DB_PATH=/opt/iris/iris.db

[Install]
WantedBy=multi-user.target
```

Back up the SQLite file nightly (`cron` copy) or continuously with litestream.

## Dev tools

- `python preview.py` renders sample charts to `preview_out/` with fake data.
- `python -m pytest tests/` runs the analysis/storage unit tests.

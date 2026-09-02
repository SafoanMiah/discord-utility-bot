# Iris

A Discord bot for our server. It keeps track of when people chat and hang out
in voice, then turns that into nice looking charts. It never stores what
anyone writes, only the times. No XP, no levels, just stats.

## Running it

You need Python 3.11 or newer.

```
python -m venv .venv
.venv/Scripts/pip install -r iris/requirements.txt   (on Linux: .venv/bin/pip)
python -m iris.bot
```

Put your settings in a `.env` file in the repo root:

```
DISCORD_TOKEN=your-bot-token
GUILD_ID=your-server-id
BACKUP_CHANNEL_ID=a-private-channel-id
```

`GUILD_ID` is optional but makes slash commands show up instantly instead of
taking up to an hour. `BACKUP_CHANNEL_ID` is optional too: when set, Iris
posts a compressed copy of its database to that channel every day (and on
`/backup`), so even if the host dies you can restore from the latest file.
Make it a private channel, and get the id by right-clicking the channel with
Discord developer mode turned on.

In the Discord Developer Portal (your app, then Bot, then Privileged Gateway
Intents) turn on **Server Members** and **Message Content**. Message Content
sounds scary but it's only there because Discord hides other bots' embeds
without it, and `/backlog vc` needs to read CircleBot's log embeds. Iris does
not read or store anyone's actual messages.

## Commands

| Command | What it does |
|---|---|
| `/stats activity @user` | Charts of when someone chats and sits in voice. Add a day to see just Fridays etc. |
| `/stats card @user` | A stat card: totals, most active hour, longest voice session and so on |
| `/timezone set` | Set your timezone, all charts then show in your local time |
| `/timezone show` / `clear` | Check or remove it |
| `/privacy optout` | Stop being tracked and delete everything Iris has on you |
| `/privacy optin` | Start being tracked again (deleted data stays deleted) |
| `/backlog chats` | Reads all channel history and fills in past message activity (managers only) |
| `/backlog vc #channel` | Rebuilds past voice sessions from CircleBot's logs in that channel (managers only) |
| `/backup` | Posts a database backup to the backup channel right now (managers only) |

Charts always use the timezone of whoever ran the command. If you haven't set
one, they're in UTC and the bot privately tells you how to set it.

Bots are never tracked. Neither are people who opted out, including in
backfills.

## Good to know

- Everything is stored as UTC timestamps in one SQLite file (`iris.db`).
  Back that file up now and then, it's the whole database.
- If the bot crashes, voice time still gets saved. It checkpoints every
  minute, so worst case you lose 60 seconds.
- Both `/backlog` commands are safe to run again, they skip anything already
  recorded instead of counting it twice. `/backlog vc` also fills in stretches
  when the bot was offline, so downtime can be recovered from CircleBot's logs
  after the fact.
- Only run one copy of the bot at a time.

## Hosting

Any always-on box with about 1 GB of RAM works. Options, best first:

- A VM you control (Oracle Cloud Always Free, Google Cloud e2-micro, a
  Raspberry Pi at home). Most reliable.
- A free bot hosting panel (bot-hosting.net, Wispbyte and similar). Fine for
  a friends server, but set `BACKUP_CHANNEL_ID` first: free panels can wipe
  files or disappear, and the daily Discord backup is what makes that
  survivable. `main.py` at the repo root exists so panels have a file to run.

To restore from a backup: download the newest `iris-*.db.gz` from the backup
channel, un-gzip it, name it `iris.db`, put it next to the bot, start it.

On a Linux VM, run it under systemd so it restarts itself:

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

[Install]
WantedBy=multi-user.target
```

## For development

- `python preview.py` renders sample charts with fake data into `preview_out/`
  so you can tweak the look without touching Discord.
- `python -m pytest tests/` runs the tests.
- Layout: `bot.py` is Discord stuff, `storage.py` is the only file that talks
  to the database, `analysis.py` does the number crunching, `charts.py` and
  `theme.py` draw the images. If you ever move to Postgres you only rewrite
  `storage.py`.

# Iris

A Discord bot for our server. It tracks when people chat, sit in voice, and
what games they play, then draws charts from that. It never stores what anyone
writes - only the times, plus game names like "VALORANT". No XP, no levels,
just stats.

## Running it

You'll need Python 3.11 or newer.

```
python -m venv .venv
.venv/Scripts/pip install -r iris/requirements.txt   (on Linux: .venv/bin/pip)
python -m iris.bot
```

Put your settings in a `.env` file in the repo root:

```
DISCORD_TOKEN=your-bot-token
GUILD_ID=your-server-id
```

`GUILD_ID` is optional, but it makes slash commands show up instantly instead
of taking up to an hour.

Once Iris is running, set an **admin channel** with `/admin set #channel`.
Iris drops a compressed copy of its database there every day (and on
`/backup`), so if the host dies you can restore from the latest file. Other
admin messages go there too, so use a private channel. Nothing is posted until
you set it.

In the Discord Developer Portal (your app → Bot → Privileged Gateway Intents),
turn on **Server Members**, **Presence**, and **Message Content**. Presence is
how Iris sees game activity. Message Content sounds scary, but it's only needed
because Discord hides other bots' embeds without it - Iris never reads or
stores anyone's messages.

## Commands

| Command | What it does |
|---|---|
| `/stats activity @user` | Charts of when someone chats and sits in voice. Add a day to see just Fridays, etc. |
| `/stats card @user` | A stat card: totals, most active hour, longest voice session, and so on |
| `/stats games @user` | Their most-played games, ranked by time |
| `/timezone set` | Set your timezone so charts show your local time |
| `/timezone show` / `clear` | Check or remove it |
| `/privacy optout` | Stop tracking and delete everything Iris has on you |
| `/privacy optin` | Start tracking again (deleted data stays deleted) |
| `/vote` | Start a button poll. Opens a form for the title and options; pick public/anonymous and single/multiple choice |
| `/backlog chats` | Fill in past message activity from channel history (managers only) |
| `/admin set #channel` | Set the channel for backups and alerts (managers only) |
| `/admin show` | Show the current admin channel (managers only) |
| `/backup` | Post a database backup right now (managers only) |

Charts use the timezone of whoever ran the command. Without one, they're in
UTC and Iris quietly tells you how to set yours.

Bots and opted-out people are never tracked, backfills included.

### Votes

`/vote` posts a poll with a button per option; the embed tallies live as people
click. Options go one per line in the form. Add ` | ` after an option and Iris
will DM that text to whoever picks it (handy for links or instructions) —
falling back to a private reply if their DMs are closed:

```
Attend | See you Friday 7pm — here's the link: https://…
Maybe
Can't make it
```

Public votes list who chose each option; anonymous ones show only counts. When
the creator or a manager closes a vote, the final results are copied to the
admin channel (anonymous votes stay anonymous there too).

## Good to know

- Everything lives in one SQLite file (`iris.db`) as UTC timestamps. Back it
  up now and then - that's the whole database.
- Voice time survives crashes. It checkpoints every minute, so worst case you
  lose 60 seconds. Game sessions work the same way.
- `/backlog chats` is safe to re-run - it skips anything already recorded.
- Game activity can't be backfilled. Discord keeps no history of it, so
  counting only starts once Iris is running with the Presence intent on.
- Run only one copy of the bot at a time.

## Hosting

Any always-on box with ~1 GB of RAM works. Best options first:

- **A VM you control** - Oracle Cloud Always Free, Google Cloud e2-micro, or a
  Raspberry Pi at home. Most reliable.
- **A free bot-hosting panel** - bot-hosting.net, Wispbyte, and the like. Fine
  for a friends server, but set an admin channel first: free panels can wipe
  files or vanish, and the daily backup is what saves you. `main.py` at the
  repo root is there so panels have a file to run.

To restore a backup: grab the newest `iris-*.db.gz` from the admin channel,
un-gzip it, rename it `iris.db`, drop it next to the bot, and start it.

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

- `python preview.py` renders sample charts with fake data into `preview_out/`,
  so you can tweak the look without touching Discord.
- `python -m pytest tests/` runs the tests.
- Layout: `bot.py` is the Discord side, `storage.py` is the only file that
  touches the database, `analysis.py` does the number crunching, and
  `charts.py` / `theme.py` draw the images. Moving to Postgres would only mean
  rewriting `storage.py`.

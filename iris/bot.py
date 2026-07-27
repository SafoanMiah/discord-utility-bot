"""Iris entry point: Discord client, event handlers, slash commands.

Run from the repo root:  python -m iris.bot
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import re
import shutil
import signal
import tempfile
import time
from datetime import date, datetime, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import discord
from discord import app_commands
from discord.ext import tasks

from . import analysis, charts, config
from .analysis import WEEKDAYS
from .storage import Storage

log = logging.getLogger("iris")

AVAILABLE_TZS = sorted(available_timezones())
COMMON_TZS = [
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Madrid",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Toronto", "America/Sao_Paulo", "Asia/Tokyo", "Asia/Shanghai",
    "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Australia/Sydney",
    "Pacific/Auckland", "UTC",
]
UTC_NOTE = (
    "🕐 Times on this chart are in **UTC** because you haven't set a timezone.\n"
    "To see everything in your local time:\n"
    "1. Type `/timezone set`\n"
    "2. In the **zone** box, start typing your city or region (e.g. `london`)\n"
    "3. Pick yours from the list — it's saved, all future charts use it."
)

storage = Storage(config.DB_PATH)


class IrisClient(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True          # required
        intents.voice_states = True    # on_voice_state_update + VC snapshots
        intents.members = True         # privileged: joined_at + reliable snapshots
        intents.presences = True       # privileged: on_presence_update + game names
        # Privileged, and used for exactly one thing: Discord hides other
        # bots' EMBEDS without it, and /backlog vc parses CircleBot's log
        # embeds. Iris never reads or stores anyone's message text.
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.opted_out: set[int] = set()
        self._voice_recovered = False

    async def setup_hook(self) -> None:
        await storage.open()
        self.opted_out = await storage.get_opted_out_ids()
        # Re-attach button views to still-open votes so they keep working
        # across restarts (persistent views, keyed by their message id).
        for vote_id, message_id in await storage.get_open_votes():
            options = await storage.get_vote_options(vote_id)
            self.add_view(VoteView(vote_id, options), message_id=message_id)
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            # The previous bot registered its commands globally (/vc, /help…)
            # and those registrations persist server-side. Push an empty
            # global set so they disappear; Iris lives in the guild scope.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()

    # -- capture -------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if message.author.id in self.opted_out:
            return
        # Only real chat (incl. replies) — join notices, boosts, pins etc.
        # carry a member author but aren't messages they sent.
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        await storage.log_message(
            message.author.id,
            message.guild.id,
            message.channel.id,
            int(message.created_at.timestamp()),
            message_id=message.id,
        )

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot or member.id in self.opted_out:
            return
        if before.channel == after.channel:
            return  # mute/deafen/video/stream change, not a join/leave/move
        now = int(time.time())
        if before.channel is not None:
            await storage.close_voice_session(member.id, member.guild.id, now)
        if after.channel is not None:
            await storage.open_voice_session(member.id, member.guild.id, after.channel.id, now)

    async def on_presence_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        # Presence updates fire constantly (status flips, Spotify progress…);
        # we only care when the SET of games being played changes.
        if after.bot or after.id in self.opted_out:
            return
        before_games = _playing_games(before)
        after_games = _playing_games(after)
        if before_games == after_games:
            return
        now = int(time.time())
        for game in before_games - after_games:
            await storage.close_game_session(after.id, after.guild.id, game, now)
        for game in after_games - before_games:
            await storage.open_game_session(after.id, after.guild.id, game, now)

    # -- crash recovery -------------------------------------------------------

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        if self._voice_recovered:
            return
        self._voice_recovered = True
        now = int(time.time())
        stale = await storage.reconcile_open_sessions(now)
        stale_games = await storage.reconcile_open_game_sessions(now)
        if stale or stale_games:
            log.info(
                "Reconciled %d voice and %d game session(s) left open by a previous run",
                stale, stale_games,
            )
        opened = played = 0
        for guild in self.guilds:
            for channel in (*guild.voice_channels, *guild.stage_channels):
                for member in channel.members:
                    if member.bot or member.id in self.opted_out:
                        continue
                    await storage.open_voice_session(member.id, guild.id, channel.id, now)
                    opened += 1
            for member in guild.members:
                if member.bot or member.id in self.opted_out:
                    continue
                for game in _playing_games(member):
                    await storage.open_game_session(member.id, guild.id, game, now)
                    played += 1
        if opened:
            log.info("Snapshotted %d member(s) already in voice", opened)
        if played:
            log.info("Snapshotted %d game(s) already being played", played)
        if not heartbeat_loop.is_running():
            heartbeat_loop.start()
        # Always run; the loop no-ops on any day no admin channel is set.
        if not backup_loop.is_running():
            backup_loop.start()

    def members_in_voice(self) -> list[int]:
        return [
            member.id
            for guild in self.guilds
            for channel in (*guild.voice_channels, *guild.stage_channels)
            for member in channel.members
            if not member.bot and member.id not in self.opted_out
        ]

    def members_playing(self) -> list[tuple[int, str]]:
        """(user_id, game) for every game currently being played, filtered."""
        return [
            (member.id, game)
            for guild in self.guilds
            for member in guild.members
            if not member.bot and member.id not in self.opted_out
            for game in _playing_games(member)
        ]


def _playing_games(member: discord.Member) -> set[str]:
    """The named games a member is currently playing (Rich Presence). Excludes
    custom statuses, Spotify, streaming — only ActivityType.playing."""
    return {
        activity.name
        for activity in member.activities
        if activity.type is discord.ActivityType.playing and activity.name
    }


client = IrisClient()


@tasks.loop(seconds=config.HEARTBEAT_SECONDS)
async def heartbeat_loop() -> None:
    now = int(time.time())
    await storage.heartbeat(client.members_in_voice(), now)
    await storage.heartbeat_games(client.members_playing(), now)


# -- admin channel & database backups ------------------------------------------
# Free hosts can vanish or wipe files; posting the db to a private Discord
# channel means the data survives anything short of Discord itself. That
# channel — the admin channel, set with /admin set — is also where any other
# administrative messages go.


async def _admin_channel() -> discord.abc.Messageable | None:
    """The configured admin channel, or None if unset or invisible to Iris."""
    channel_id = await storage.get_admin_channel_id()
    if channel_id is None:
        return None
    channel = client.get_channel(channel_id)
    if channel is None:
        log.warning("Admin channel %s is not a channel I can see", channel_id)
    return channel

def _gzip_file(src: Path, dest: Path) -> None:
    with open(src, "rb") as fin, gzip.open(dest, "wb") as fout:
        shutil.copyfileobj(fin, fout)


async def _make_backup_file() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    raw = Path(tempfile.gettempdir()) / f"iris-{stamp}.db"
    raw.unlink(missing_ok=True)  # VACUUM INTO refuses to overwrite
    await storage.backup_to(str(raw))
    packed = raw.with_suffix(".db.gz")
    await asyncio.to_thread(_gzip_file, raw, packed)
    raw.unlink()
    return packed


async def _post_backup(reason: str) -> discord.Message | None:
    channel = await _admin_channel()
    if channel is None:
        return None
    packed = await _make_backup_file()
    try:
        return await channel.send(
            f"🗃️ Database backup ({reason}). To restore: download, un-gzip, "
            "and start the bot with it as `iris.db`.",
            file=discord.File(packed),
        )
    finally:
        packed.unlink(missing_ok=True)


@tasks.loop(time=dtime(hour=4, tzinfo=timezone.utc))
async def backup_loop() -> None:
    try:
        await _post_backup("daily")
    except Exception:
        log.exception("Daily backup failed")


@client.tree.command(name="backup", description="Post a database backup to the admin channel now")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def backup_cmd(interaction: discord.Interaction) -> None:
    if await storage.get_admin_channel_id() is None:
        await interaction.response.send_message(
            "No admin channel set. An admin can set one with `/admin set` — database "
            "backups and other administrative messages go there.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    message = await _post_backup("manual")
    if message is None:
        await interaction.followup.send(
            "I can't see the admin channel — check my permissions there, or point me "
            "at a new one with `/admin set`.", ephemeral=True,
        )
    else:
        await interaction.followup.send(f"Backup posted: {message.jump_url}", ephemeral=True)


# -- /admin -------------------------------------------------------------------

admin_group = app_commands.Group(
    name="admin",
    description="Bot administration (server managers only)",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@admin_group.command(
    name="set",
    description="Set the channel for administrative messages (daily DB backups, alerts)",
)
@app_commands.describe(channel="Channel Iris posts administrative messages to")
async def admin_set(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages):
        await interaction.response.send_message(
            f"I can't post in {channel.mention} — grant me **View Channel** and "
            "**Send Messages** there first.", ephemeral=True,
        )
        return
    await storage.set_admin_channel_id(channel.id)
    await interaction.response.send_message(
        f"Admin channel set to {channel.mention}. Daily database backups and other "
        "administrative messages will go there.", ephemeral=True,
    )


@admin_group.command(name="show", description="Show the current admin channel")
async def admin_show(interaction: discord.Interaction) -> None:
    channel_id = await storage.get_admin_channel_id()
    if channel_id is None:
        await interaction.response.send_message(
            "No admin channel set — use `/admin set`.", ephemeral=True
        )
        return
    channel = client.get_channel(channel_id)
    where = channel.mention if channel else f"a channel I can't currently see (id `{channel_id}`)"
    await interaction.response.send_message(f"Admin channel is {where}.", ephemeral=True)


client.tree.add_command(admin_group)


# -- shared helpers -----------------------------------------------------------

async def _requester_tz(user_id: int) -> tuple[ZoneInfo, str, str | None]:
    """(tzinfo, label, note) — note is the UTC hint when the requester is unset."""
    name = await storage.get_timezone(user_id)
    if name:
        try:
            return ZoneInfo(name), name, None
        except KeyError:
            pass
    return ZoneInfo("UTC"), "UTC", UTC_NOTE


async def _fetch_activity(user_id: int, guild_id: int) -> tuple[list[int], list[tuple[int, int]]]:
    msgs = [ts for _, ts in await storage.get_messages(user_id, guild_id)]
    sessions = [(s, e) for _, s, e in await storage.get_voice_sessions(user_id, guild_id)]
    return msgs, sessions


def _fmt_date(d: date | None) -> str:
    return f"{d.day} {d:%b %Y}" if d else "—"


def _fmt_last_active(epoch: int | None, tz: ZoneInfo) -> str:
    if epoch is None:
        return "—"
    days = (datetime.now(tz).date() - datetime.fromtimestamp(epoch, tz).date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    return _fmt_date(datetime.fromtimestamp(epoch, tz).date())


def _build_activity_png(name, msgs, sessions, tz, tz_label, day_index):
    """Sync: aggregation + render, run via asyncio.to_thread."""
    msg_grid = analysis.message_grid(msgs, tz)
    vc_grid = analysis.voice_grid(sessions, tz)
    if day_index is None:
        return charts.render_activity(
            name, f"Activity · all time · times in {tz_label}",
            analysis.hour_totals(msg_grid), analysis.hour_totals(vc_grid),
            analysis.weekday_totals(msg_grid), analysis.weekday_totals(vc_grid),
        )
    return charts.render_activity_day(
        name, f"Activity · {WEEKDAYS[day_index]}s · times in {tz_label}",
        analysis.day_slice(msg_grid, day_index), analysis.day_slice(vc_grid, day_index),
    )


def _build_games_png(name, game_sessions):
    """Sync: aggregation + render, run via asyncio.to_thread. game_sessions is
    a list of (game, start_utc, end_utc); totals are timezone-independent."""
    return charts.render_games(
        name, "Top games · all time", analysis.game_totals(game_sessions)
    )


def _build_stats_png(name, msgs, sessions, tz, tz_label, joined: date | None):
    """Sync: aggregation + render, run via asyncio.to_thread."""
    s = analysis.summary(msgs, sessions, tz)
    per_msg = s["vc_seconds_per_message"]
    has_vc = s["session_count"] > 0
    hero = [
        ("Messages", charts.fmt_count(s["total_messages"])),
        ("Voice time", charts.fmt_duration(s["total_vc_seconds"])),
        ("Active days", charts.fmt_count(s["active_days"])),
    ]
    details = [
        ("Most active hour",
         charts.fmt_hour_range(s["most_active_hour"]) if s["most_active_hour"] is not None else "—"),
        ("Most active day",
         WEEKDAYS[s["most_active_weekday"]] if s["most_active_weekday"] is not None else "—"),
        ("Voice per message",
         f"{per_msg / 60:.1f} min" if per_msg is not None and has_vc else "—"),
        ("Longest voice session",
         charts.fmt_duration(s["longest_session_seconds"]) if has_vc else "—"),
        ("Avg voice session",
         charts.fmt_duration(s["avg_session_seconds"]) if has_vc else "—"),
        ("Tracked since", _fmt_date(s["tracked_since"])),
        ("Joined server", _fmt_date(joined)),
        ("Last active", _fmt_last_active(s["last_active_utc"], tz)),
    ]
    return charts.render_stats_card(name, f"Stats · all time · times in {tz_label}", hero, details)


# -- /timezone ----------------------------------------------------------------

tz_group = app_commands.Group(
    name="timezone", description="Your timezone for activity views", guild_only=True
)


async def _tz_autocomplete(_: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    needle = current.strip().lower()
    matches = COMMON_TZS if not needle else [z for z in AVAILABLE_TZS if needle in z.lower()]
    return [app_commands.Choice(name=z, value=z) for z in matches[:25]]


@tz_group.command(name="set", description="Set your timezone (IANA name)")
@app_commands.describe(zone="e.g. Europe/London — start typing to search")
@app_commands.autocomplete(zone=_tz_autocomplete)
async def tz_set(interaction: discord.Interaction, zone: str) -> None:
    if zone not in AVAILABLE_TZS:
        await interaction.response.send_message(
            f"`{zone}` isn't a known IANA timezone. Pick one from the autocomplete "
            "(offsets like `GMT+8` aren't supported).",
            ephemeral=True,
        )
        return
    await storage.set_timezone(interaction.user.id, zone)
    now = datetime.now(ZoneInfo(zone))
    await interaction.response.send_message(
        f"Timezone set to **{zone}** — your local time is {now:%H:%M}.", ephemeral=True
    )


@tz_group.command(name="show", description="Show your current timezone")
async def tz_show(interaction: discord.Interaction) -> None:
    name = await storage.get_timezone(interaction.user.id)
    text = f"Your timezone is **{name}**." if name else "Not set — using **UTC**."
    await interaction.response.send_message(text, ephemeral=True)


@tz_group.command(name="clear", description="Clear your timezone (fall back to UTC)")
async def tz_clear(interaction: discord.Interaction) -> None:
    await storage.clear_timezone(interaction.user.id)
    await interaction.response.send_message("Timezone cleared — using **UTC**.", ephemeral=True)


client.tree.add_command(tz_group)


# -- /stats -------------------------------------------------------------------

stats_group = app_commands.Group(
    name="stats", description="Activity charts and stat cards", guild_only=True
)


async def _send_chart(
    interaction: discord.Interaction, png, filename: str, note: str | None
) -> None:
    """Post the chart publicly; if the requester has no timezone set, follow
    with an ephemeral how-to only they can see."""
    await interaction.followup.send(file=discord.File(png, filename=filename))
    if note:
        await interaction.followup.send(note, ephemeral=True)


async def _target_data(
    interaction: discord.Interaction, user: discord.Member
) -> tuple[list[int], list[tuple[int, int]]] | None:
    """Defer, validate the target, and fetch their rows. Returns None (with
    the response already sent) when there's nothing to render."""
    await interaction.response.defer()
    if user.bot:
        await interaction.followup.send("Bots aren't tracked.")
        return None
    if await storage.is_opted_out(user.id):
        await interaction.followup.send("No data — this user has opted out.")
        return None
    msgs, sessions = await _fetch_activity(user.id, interaction.guild_id)
    if not msgs and not sessions:
        await interaction.followup.send(f"No activity recorded for **{user.display_name}** yet.")
        return None
    return msgs, sessions


@stats_group.command(name="activity", description="Activity charts for a member")
@app_commands.describe(user="Member to view", day="Only show one weekday")
@app_commands.choices(day=[app_commands.Choice(name=d, value=i) for i, d in enumerate(WEEKDAYS)])
async def stats_activity(
    interaction: discord.Interaction,
    user: discord.Member,
    day: app_commands.Choice[int] | None = None,
) -> None:
    data = await _target_data(interaction, user)
    if data is None:
        return
    msgs, sessions = data
    tz, tz_label, note = await _requester_tz(interaction.user.id)
    day_index = day.value if day is not None else None
    png = await asyncio.to_thread(
        _build_activity_png, user.display_name, msgs, sessions, tz, tz_label, day_index
    )
    await _send_chart(interaction, png, "activity.png", note)


@stats_group.command(name="card", description="Stats card for a member")
@app_commands.describe(user="Member to view")
async def stats_card(interaction: discord.Interaction, user: discord.Member) -> None:
    data = await _target_data(interaction, user)
    if data is None:
        return
    msgs, sessions = data
    tz, tz_label, note = await _requester_tz(interaction.user.id)
    joined = user.joined_at.date() if user.joined_at else None
    png = await asyncio.to_thread(
        _build_stats_png, user.display_name, msgs, sessions, tz, tz_label, joined
    )
    await _send_chart(interaction, png, "stats.png", note)


@stats_group.command(name="games", description="Most-played games for a member")
@app_commands.describe(user="Member to view")
async def stats_games(interaction: discord.Interaction, user: discord.Member) -> None:
    await interaction.response.defer()
    if user.bot:
        await interaction.followup.send("Bots aren't tracked.")
        return
    if await storage.is_opted_out(user.id):
        await interaction.followup.send("No data — this user has opted out.")
        return
    game_sessions = await storage.get_game_sessions(user.id, interaction.guild_id)
    if not game_sessions:
        await interaction.followup.send(
            f"No game activity recorded for **{user.display_name}** yet."
        )
        return
    png = await asyncio.to_thread(_build_games_png, user.display_name, game_sessions)
    await interaction.followup.send(file=discord.File(png, filename="games.png"))


client.tree.add_command(stats_group)


# -- /privacy -----------------------------------------------------------------

privacy_group = app_commands.Group(
    name="privacy", description="Control what Iris logs about you", guild_only=True
)


@privacy_group.command(name="optout", description="Stop logging you and delete your history")
async def privacy_optout(interaction: discord.Interaction) -> None:
    await storage.set_optout(interaction.user.id)
    client.opted_out.add(interaction.user.id)
    await interaction.response.send_message(
        "Opted out. Your recorded messages, voice sessions, and game activity "
        "have been deleted, and Iris will no longer log you.",
        ephemeral=True,
    )


@privacy_group.command(name="optin", description="Resume logging (deleted history is gone)")
async def privacy_optin(interaction: discord.Interaction) -> None:
    await storage.set_optin(interaction.user.id)
    client.opted_out.discard(interaction.user.id)
    # If they're in voice or playing something right now, start tracking those
    # immediately rather than waiting for the next join/presence change.
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if member:
        now = int(time.time())
        if member.voice and member.voice.channel:
            await storage.open_voice_session(
                member.id, interaction.guild_id, member.voice.channel.id, now
            )
        for game in _playing_games(member):
            await storage.open_game_session(member.id, interaction.guild_id, game, now)
    await interaction.response.send_message(
        "Opted in — Iris will log your activity from now on. "
        "Previously deleted history is not restored.",
        ephemeral=True,
    )


client.tree.add_command(privacy_group)


# -- /backlog -----------------------------------------------------------------

backlog_group = app_commands.Group(
    name="backlog",
    description="Backfill historical data (server managers only)",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)

_backfills_running: set[int] = set()


@backlog_group.command(
    name="chats",
    description="Scan every text channel's history and log past message activity",
)
async def backlog_chats(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild.id in _backfills_running:
        await interaction.response.send_message(
            "A backfill is already running for this server.", ephemeral=True
        )
        return
    await interaction.response.defer()
    _backfills_running.add(guild.id)
    try:
        await _run_chat_backfill(interaction)
    finally:
        _backfills_running.discard(guild.id)


def _progress_editor(interaction: discord.Interaction):
    """Edits the deferred response, time-gated so channel-by-channel updates
    never hit rate limits, and never raises: on very long runs the
    interaction token can expire."""
    last_edit = 0.0

    async def progress(text: str, force: bool = False) -> None:
        nonlocal last_edit
        if not force and time.monotonic() - last_edit < 4:
            return
        last_edit = time.monotonic()
        try:
            await interaction.edit_original_response(content=text)
        except discord.HTTPException:
            pass

    return progress


async def _run_chat_backfill(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    channels = list(guild.text_channels)
    inserted = duplicates = 0
    skipped: list[str] = []
    progress = _progress_editor(interaction)

    async def flush(batch: list[tuple[int, int, int, int, int]]) -> None:
        nonlocal inserted, duplicates
        added = await storage.log_messages_bulk(batch)
        inserted += added
        duplicates += len(batch) - added
        batch.clear()

    for i, channel in enumerate(channels, 1):
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.read_message_history):
            skipped.append(f"#{channel.name}")
            continue
        batch: list[tuple[int, int, int, int, int]] = []
        try:
            async for msg in channel.history(limit=None, oldest_first=True):
                if msg.author.bot or msg.author.id in client.opted_out:
                    continue  # NEVER log bots; respect opt-outs historically too
                if msg.type not in (discord.MessageType.default, discord.MessageType.reply):
                    continue
                batch.append((msg.id, msg.author.id, guild.id, channel.id,
                              int(msg.created_at.timestamp())))
                if len(batch) >= 1000:
                    await flush(batch)
                    await progress(
                        f"Backfilling **#{channel.name}** ({i}/{len(channels)}) — "
                        f"{inserted:,} messages logged so far…"
                    )
        except discord.Forbidden:
            skipped.append(f"#{channel.name}")
            continue
        await flush(batch)
        # This channel is now fully re-recorded with message ids, so rows
        # live-captured before id tracking existed are safe to drop.
        await storage.purge_legacy_messages(guild.id, channel.id)
        await progress(
            f"Backfilling… **#{channel.name}** done ({i}/{len(channels)}), "
            f"{inserted:,} messages logged so far."
        )

    lines = [
        f"Backlog complete: logged **{inserted:,}** new messages across "
        f"{len(channels) - len(skipped)} channels."
    ]
    if duplicates:
        lines.append(f"{duplicates:,} were already recorded and skipped.")
    if skipped:
        lines.append("No history access to: " + ", ".join(skipped) + ".")
    lines.append("Bot messages and opted-out members were excluded.")
    await progress("\n".join(lines), force=True)


# /backlog vc is disabled — the CircleBot import was a one-time rebuild that's
# already done. The command below is commented out so it no longer registers;
# the storage/analysis helpers it used (reconstruct_sessions,
# delete_voice_sessions_by_source, …) stay in place and remain tested. Uncomment
# this whole block to bring the command back.
#
# _USER_ID_RE = re.compile(r"User ID:\s*(\d{15,21})")
# _MENTION_RE = re.compile(r"<@!?(\d{15,21})>")
# _CHANNEL_MENTION_RE = re.compile(r"<#(\d{15,21})>")
#
#
# def _parse_circle_embed(
#     embed: discord.Embed, message: discord.Message, vc_name_to_id: dict[str, int]
# ) -> tuple[int, int, int, str] | None:
#     """One CircleBot log embed -> (ts_utc, user_id, channel_id, kind), or None
#     for entries that aren't voice joins/leaves (nick changes, server joins…)."""
#     text = " ".join(filter(None, (embed.title, embed.description)))
#     if "has joined a voice channel" in text:
#         kind = "join"
#     elif "has left a voice channel" in text:
#         kind = "leave"
#     else:
#         return None
#
#     match = _USER_ID_RE.search(embed.footer.text or "") or _MENTION_RE.search(text)
#     if not match:
#         return None
#     user_id = int(match.group(1))
#
#     channel_id = 0  # unknown/renamed channels still count toward totals
#     for field in embed.fields:
#         if (field.name or "").strip().lower() == "channel":
#             value = (field.value or "").strip()
#             chan_match = _CHANNEL_MENTION_RE.search(value)
#             channel_id = int(chan_match.group(1)) if chan_match else vc_name_to_id.get(value, 0)
#             break
#
#     when = embed.timestamp or message.created_at
#     return (int(when.timestamp()), user_id, channel_id, kind)
#
#
# @backlog_group.command(
#     name="vc",
#     description="Rebuild past voice sessions from CircleBot's log history",
# )
# @app_commands.describe(channel="The channel where CircleBot posts its logs")
# async def backlog_vc(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
#     guild = interaction.guild
#     if guild.id in _backfills_running:
#         await interaction.response.send_message(
#             "A backfill is already running for this server.", ephemeral=True
#         )
#         return
#     perms = channel.permissions_for(guild.me)
#     if not (perms.view_channel and perms.read_message_history):
#         await interaction.response.send_message(
#             f"I can't read the history of {channel.mention} — grant me "
#             "**View Channel** and **Read Message History** there first.",
#             ephemeral=True,
#         )
#         return
#     await interaction.response.defer()
#     _backfills_running.add(guild.id)
#     try:
#         await _run_vc_backfill(interaction, channel)
#     finally:
#         _backfills_running.discard(guild.id)
#
#
# async def _run_vc_backfill(
#     interaction: discord.Interaction, channel: discord.TextChannel
# ) -> None:
#     guild = interaction.guild
#     progress = _progress_editor(interaction)
#     vc_name_to_id = {
#         ch.name: ch.id for ch in (*guild.voice_channels, *guild.stage_channels)
#     }
#
#     events: list[tuple[int, int, int, str]] = []
#     scanned = embeds_seen = 0
#     async for msg in channel.history(limit=None, oldest_first=True):
#         if msg.author.id != config.CIRCLEBOT_ID:
#             continue
#         scanned += 1
#         for embed in msg.embeds:
#             embeds_seen += 1
#             event = _parse_circle_embed(embed, msg, vc_name_to_id)
#             if event is not None:
#                 events.append(event)
#         if scanned % 500 == 0:
#             await progress(
#                 f"Reading CircleBot logs in {channel.mention}… {scanned:,} messages "
#                 f"scanned, {len(events):,} voice events found."
#             )
#
#     if scanned == 0:
#         await progress(
#             f"No CircleBot messages found in {channel.mention} — is that the right "
#             "log channel?", force=True,
#         )
#         return
#     if embeds_seen == 0:
#         await progress(
#             f"Found {scanned:,} CircleBot messages but Discord returned no embed "
#             "data. Enable **Message Content Intent** in the Developer Portal "
#             "(Bot → Privileged Gateway Intents) and restart Iris.", force=True,
#         )
#         return
#
#     # NEVER log bots; respect opt-outs even for historical data.
#     def tracked(user_id: int) -> bool:
#         if user_id in client.opted_out:
#             return False
#         member = guild.get_member(user_id)
#         return member is None or not member.bot
#
#     events = [e for e in events if tracked(e[1])]
#
#     cutoff = await storage.earliest_live_voice_start(guild.id)
#     sessions, ignored = analysis.reconstruct_sessions(events, cutoff)
#     replaced = await storage.delete_voice_sessions_by_source(guild.id, "backlog")
#     added = await storage.add_voice_sessions_bulk(guild.id, sessions)
#
#     users = len({s[0] for s in sessions})
#     lines = [
#         f"Voice backlog complete: rebuilt **{added:,}** sessions for {users} members "
#         f"from {len(events):,} join/leave events."
#     ]
#     if replaced:
#         lines.append(f"Replaced {replaced:,} sessions from a previous import.")
#     if cutoff:
#         lines.append("Events after Iris's own voice tracking began were skipped "
#                      "so nothing is counted twice.")
#     if ignored:
#         lines.append(f"{ignored:,} events couldn't be paired (log gaps) and were ignored.")
#     lines.append("Bots and opted-out members were excluded.")
#     await progress("\n".join(lines), force=True)


client.tree.add_command(backlog_group)


# -- /vote --------------------------------------------------------------------
# A button poll. The command opens a modal for the title + options; each option
# becomes a button. Results live in the message embed and update on every click.
# An option line is "Label | RoleID | Message": the role (if given) is granted
# while that option is selected and dropped when it's deselected, and the
# message (if given) is shown privately to the voter. Everything persists in the
# db, so votes survive restarts, and closing a vote archives it to the admin
# channel.

MAX_VOTE_OPTIONS = 20         # 20 option buttons + a close button ≤ 25 per view
_OPT_DELIM = " | "
_LABEL_STORE_MAX = 200        # generous for the embed; button labels cap at 80
_BUTTON_LABEL_MAX = 80        # Discord's hard limit
_NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def _num(idx: int) -> str:
    return _NUM_EMOJI[idx] if idx < len(_NUM_EMOJI) else f"{idx + 1}."


def _parse_vote_options(
    raw: str,
) -> tuple[list[tuple[str, int | None, str | None]], str | None]:
    """Turn the modal's options box into (label, role_id, message) triples, one
    per line as `Label | RoleID | Message` (role id and message optional; the
    role id may be a raw id or an @role mention). Returns (options, error);
    error is a user-facing message when the input is invalid. Blank lines are
    ignored and duplicate labels dropped."""
    options: list[tuple[str, int | None, str | None]] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(_OPT_DELIM, 2)]
        label = parts[0]
        role_part = parts[1] if len(parts) > 1 else ""
        message = parts[2] if len(parts) > 2 else ""
        if not label or label.casefold() in seen:
            continue
        role_id: int | None = None
        if role_part:
            digits = re.sub(r"\D", "", role_part)  # accepts a raw id or <@&id>
            if not digits:
                return [], (
                    f"“{role_part}” isn't a valid role — use the role's id or an "
                    "@role mention (turn on Developer Mode to copy an id)."
                )
            role_id = int(digits)
        seen.add(label.casefold())
        options.append((label[:_LABEL_STORE_MAX], role_id, message or None))
    if len(options) < 2:
        return [], "A vote needs at least 2 options — put one per line."
    if len(options) > MAX_VOTE_OPTIONS:
        return [], f"That's too many options — {MAX_VOTE_OPTIONS} is the max."
    return options, None


def _bar(count: int, top: int, width: int = 10) -> str:
    filled = round(width * count / top) if top else 0
    return "▰" * filled + "▱" * (width - filled)


def _join_mentions(user_ids: list[int], budget: int = 900) -> str:
    """Space-joined <@id> mentions, truncated to stay well under the 1024-char
    embed field limit with a '+N more' tail."""
    parts: list[str] = []
    used = 0
    for i, uid in enumerate(user_ids):
        mention = f"<@{uid}>"
        if used + len(mention) + 1 > budget:
            parts.append(f"… +{len(user_ids) - i} more")
            break
        parts.append(mention)
        used += len(mention) + 1
    return " ".join(parts)


def _vote_embed(
    vote: dict,
    options: list[tuple[int, str, int | None, str | None]],
    tally: dict[int, list[int]],
) -> discord.Embed:
    anonymous, closed = bool(vote["anonymous"]), bool(vote["closed"])
    counts = {idx: len(tally.get(idx, [])) for idx, *_ in options}
    top = max(counts.values(), default=0)
    voters = {uid for users in tally.values() for uid in users}

    embed = discord.Embed(
        title=f"🗳️ {vote['title']}",
        color=0x99AAB5 if closed else 0x5865F2,
    )
    if closed:
        embed.description = "🔒 This vote is closed."
    elif vote["multiple"]:
        embed.description = "Click any options to vote — toggle as many as you like."
    else:
        embed.description = "Click an option to vote — click again to change or clear it."

    for idx, label, _role, _msg in options:
        count = counts[idx]
        value = f"`{_bar(count, top)}` **{count}**"
        if not anonymous and tally.get(idx):
            value += f"\n{_join_mentions(tally[idx])}"
        embed.add_field(name=f"{_num(idx)} {label}"[:256], value=value, inline=False)

    kind = "Multiple choice" if vote["multiple"] else "Single choice"
    privacy = "Anonymous" if anonymous else "Public"
    people = f"{len(voters)} {'person' if len(voters) == 1 else 'people'} voted"
    embed.set_footer(text=f"{privacy} · {kind} · {people}")
    return embed


class VoteView(discord.ui.View):
    """Persistent view: one secondary button per option plus a close button.
    Stateless beyond the vote id encoded in each button's custom_id, so it can
    be rebuilt from the database after a restart."""

    def __init__(
        self,
        vote_id: int,
        options: list[tuple[int, str, int | None, str | None]],
        closed: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        for idx, label, _role, _msg in options:
            button = discord.ui.Button(
                label=f"{idx + 1}. {label}"[:_BUTTON_LABEL_MAX],
                style=discord.ButtonStyle.secondary,
                custom_id=f"v:{vote_id}:{idx}",
                disabled=closed,
            )
            button.callback = self._on_option
            self.add_item(button)
        close = discord.ui.Button(
            label="Close vote", emoji="🔒",
            style=discord.ButtonStyle.danger,
            custom_id=f"v:{vote_id}:close", disabled=closed,
        )
        close.callback = self._on_close
        self.add_item(close)

    async def _on_option(self, interaction: discord.Interaction) -> None:
        await _handle_vote_click(interaction)

    async def _on_close(self, interaction: discord.Interaction) -> None:
        await _handle_vote_close(interaction)


def _vote_id_from(interaction: discord.Interaction) -> tuple[int, str]:
    _, vote_id, tail = interaction.data["custom_id"].split(":")
    return int(vote_id), tail


async def _sync_vote_roles(
    interaction: discord.Interaction,
    options: list[tuple[int, str, int | None, str | None]],
    user_idxs: set[int],
) -> str | None:
    """Make the voter's roles match their current selections: grant the role of
    every option they've picked and strip this vote's other roles. Returns a
    short note for the ephemeral reply, or None if nothing changed."""
    member, guild = interaction.user, interaction.guild
    if guild is None or not isinstance(member, discord.Member):
        return None
    vote_role_ids = {rid for _i, _l, rid, _m in options if rid}
    if not vote_role_ids:
        return None
    want_ids = {rid for idx, _l, rid, _m in options if rid and idx in user_idxs}
    have_ids = {r.id for r in member.roles}
    add = [role for rid in (want_ids - have_ids) if (role := guild.get_role(rid))]
    drop = [role for rid in ((vote_role_ids - want_ids) & have_ids) if (role := guild.get_role(rid))]
    if not add and not drop:
        return None
    try:
        if add:
            await member.add_roles(*add, reason="Iris vote selection")
        if drop:
            await member.remove_roles(*drop, reason="Iris vote deselection")
    except discord.Forbidden:
        return ("⚠️ I couldn't update your roles — I need **Manage Roles**, and the "
                "role must sit below my highest role.")
    notes = []
    if add:
        notes.append("➕ " + ", ".join(r.mention for r in add))
    if drop:
        notes.append("➖ " + ", ".join(r.mention for r in drop))
    return " · ".join(notes)


async def _handle_vote_click(interaction: discord.Interaction) -> None:
    vote_id, tail = _vote_id_from(interaction)
    # Acknowledge before any DB work: commits on slow hosts can exceed Discord's
    # 3-second response window (that was the "Unknown interaction" crash).
    await interaction.response.defer()
    vote = await storage.get_vote(vote_id)
    if vote is None or vote["closed"]:
        await interaction.followup.send(
            "This vote is closed or no longer exists.", ephemeral=True
        )
        return

    idx = int(tail)
    options = await storage.get_vote_options(vote_id)
    action = await storage.cast_ballot(vote_id, interaction.user.id, idx, bool(vote["multiple"]))
    tally = await storage.get_ballots(vote_id)
    await interaction.edit_original_response(
        embed=_vote_embed(vote, options, tally), view=VoteView(vote_id, options)
    )

    label = next((lbl for i, lbl, _r, _m in options if i == idx), "that option")
    message = next((msg for i, _l, _r, msg in options if i == idx), None)
    user_idxs = {i for i, users in tally.items() if interaction.user.id in users}
    role_note = await _sync_vote_roles(interaction, options, user_idxs)

    lines = [f"Removed your vote for **{label}**." if action == "removed"
             else f"You voted for **{label}**."]
    if action != "removed" and message:
        lines.append(message)
    if role_note:
        lines.append(role_note)
    await interaction.followup.send("\n".join(lines), ephemeral=True)


async def _handle_vote_close(interaction: discord.Interaction) -> None:
    vote_id, _ = _vote_id_from(interaction)
    vote = await storage.get_vote(vote_id)
    if vote is None:
        await interaction.response.send_message("This vote no longer exists.", ephemeral=True)
        return
    if vote["closed"]:
        await interaction.response.send_message("This vote is already closed.", ephemeral=True)
        return
    if interaction.user.id != vote["creator_id"] and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "Only the vote's creator or a server manager can close it.", ephemeral=True
        )
        return

    await interaction.response.defer()
    await storage.close_vote(vote_id)
    vote["closed"] = 1
    options = await storage.get_vote_options(vote_id)
    tally = await storage.get_ballots(vote_id)
    await interaction.edit_original_response(
        embed=_vote_embed(vote, options, tally), view=VoteView(vote_id, options, closed=True)
    )
    await _archive_vote_results(interaction, vote, options, tally)


async def _archive_vote_results(
    interaction: discord.Interaction,
    vote: dict,
    options: list[tuple[int, str, int | None, str | None]],
    tally: dict[int, list[int]],
) -> None:
    """Post the final results to the admin channel; nudge the closer to set one
    if it's missing. Anonymous votes stay anonymous in the copy too."""
    channel = await _admin_channel()
    if channel is None:
        await interaction.followup.send(
            "Vote closed. No admin channel is set, so I couldn't archive the results — "
            "set one with `/admin set` and future results will be copied there.",
            ephemeral=True,
        )
        return
    jump = interaction.message.jump_url if interaction.message else ""
    try:
        await channel.send(
            content=f"🗳️ **Vote results** — closed by {interaction.user.mention}"
            + (f"\n{jump}" if jump else ""),
            embed=_vote_embed(vote, options, tally),
        )
    except discord.HTTPException:
        await interaction.followup.send(
            "Vote closed, but I couldn't post to the admin channel — check my "
            "permissions there.", ephemeral=True,
        )
        return
    await interaction.followup.send(
        "Vote closed — results archived to the admin channel.", ephemeral=True
    )


class VoteModal(discord.ui.Modal, title="Create a vote"):
    vote_title = discord.ui.TextInput(
        label="Title", placeholder="What are we deciding?", max_length=256
    )
    options = discord.ui.TextInput(
        label="Options (Label | RoleID | Message) — 1/line",
        style=discord.TextStyle.paragraph,
        placeholder="Attend | 123456789012 | See you Friday!\nMaybe\nCan't make it",
        max_length=4000,
    )

    def __init__(self, anonymous: bool, multiple: bool) -> None:
        super().__init__()
        self.anonymous = anonymous
        self.multiple = multiple

    async def on_submit(self, interaction: discord.Interaction) -> None:
        options, error = _parse_vote_options(str(self.options))
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        # Ack before touching the DB: commits on slow hosts can blow past the
        # 3-second interaction window (that was the "Unknown interaction" crash).
        await interaction.response.defer(thinking=True)
        vote_id = await storage.create_vote(
            interaction.guild_id, interaction.channel_id, interaction.user.id,
            str(self.vote_title).strip(), self.anonymous, self.multiple, options, int(time.time()),
        )
        vote = await storage.get_vote(vote_id)
        opt_rows = await storage.get_vote_options(vote_id)
        message = await interaction.edit_original_response(
            embed=_vote_embed(vote, opt_rows, {}), view=VoteView(vote_id, opt_rows)
        )
        await storage.set_vote_message(vote_id, message.id)


@client.tree.command(
    name="vote",
    description="Start a button poll — opens a form for the title and options",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@app_commands.describe(
    visibility="Show who voted or keep it anonymous (default: public)",
    mode="Allow one choice each or several (default: single choice)",
)
@app_commands.choices(
    visibility=[
        app_commands.Choice(name="Public — show who voted", value="public"),
        app_commands.Choice(name="Anonymous — counts only", value="anon"),
    ],
    mode=[
        app_commands.Choice(name="Single choice", value="single"),
        app_commands.Choice(name="Multiple choice", value="multi"),
    ],
)
async def vote_cmd(
    interaction: discord.Interaction,
    visibility: app_commands.Choice[str] | None = None,
    mode: app_commands.Choice[str] | None = None,
) -> None:
    anonymous = visibility is not None and visibility.value == "anon"
    multiple = mode is not None and mode.value == "multi"
    await interaction.response.send_modal(VoteModal(anonymous, multiple))


@client.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.CommandNotFound):
        # A stale registration from the old bot; the empty global sync in
        # setup_hook removes these — the client menu just hasn't refreshed.
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "That command isn't part of Iris any more. It should vanish from "
                "the menu shortly — refreshing Discord (Ctrl+R) speeds it up.",
                ephemeral=True,
            )
        return
    log.exception("Command %s failed", interaction.command and interaction.command.name,
                  exc_info=error)
    text = "Something went wrong running that command."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# -- lifecycle ----------------------------------------------------------------

async def main() -> None:
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set (env var or .env).")
    discord.utils.setup_logging(level=logging.INFO)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(client.close()))
        except NotImplementedError:
            pass  # Windows dev box; systemd on Linux delivers SIGTERM fine

    async with client:
        try:
            await client.start(config.DISCORD_TOKEN)
        except discord.PrivilegedIntentsRequired:
            raise SystemExit(
                "Discord refused the connection: enable ALL THREE of 'Server "
                "Members Intent', 'Presence Intent', and 'Message Content "
                "Intent' in the Developer Portal (your app -> Bot -> Privileged "
                "Gateway Intents), then restart."
            )
        finally:
            # Graceful shutdown: everyone in VC "leaves" and every open game
            # "stops" now, so nothing is left open and reconcile has nothing to
            # guess at next boot.
            heartbeat_loop.cancel()
            now = int(time.time())
            await storage.close_all_open_sessions(now)
            await storage.close_all_open_game_sessions(now)
            await storage.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

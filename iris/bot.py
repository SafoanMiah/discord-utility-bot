"""Iris entry point: Discord client, event handlers, slash commands.

Run from the repo root:  python -m iris.bot
"""
from __future__ import annotations

import asyncio
import logging
import re
import signal
import time
from datetime import date, datetime
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

    # -- crash recovery -------------------------------------------------------

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        if self._voice_recovered:
            return
        self._voice_recovered = True
        now = int(time.time())
        stale = await storage.reconcile_open_sessions(now)
        if stale:
            log.info("Reconciled %d session(s) left open by a previous run", stale)
        opened = 0
        for guild in self.guilds:
            for channel in (*guild.voice_channels, *guild.stage_channels):
                for member in channel.members:
                    if member.bot or member.id in self.opted_out:
                        continue
                    await storage.open_voice_session(member.id, guild.id, channel.id, now)
                    opened += 1
        if opened:
            log.info("Snapshotted %d member(s) already in voice", opened)
        if not heartbeat_loop.is_running():
            heartbeat_loop.start()

    def members_in_voice(self) -> list[int]:
        return [
            member.id
            for guild in self.guilds
            for channel in (*guild.voice_channels, *guild.stage_channels)
            for member in channel.members
            if not member.bot and member.id not in self.opted_out
        ]


client = IrisClient()


@tasks.loop(seconds=config.HEARTBEAT_SECONDS)
async def heartbeat_loop() -> None:
    await storage.heartbeat(client.members_in_voice(), int(time.time()))


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
        "Opted out. Your recorded messages and voice sessions have been deleted, "
        "and Iris will no longer log you.",
        ephemeral=True,
    )


@privacy_group.command(name="optin", description="Resume logging (deleted history is gone)")
async def privacy_optin(interaction: discord.Interaction) -> None:
    await storage.set_optin(interaction.user.id)
    client.opted_out.discard(interaction.user.id)
    # If they're sitting in a voice channel right now, start tracking immediately.
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if member and member.voice and member.voice.channel:
        await storage.open_voice_session(
            member.id, interaction.guild_id, member.voice.channel.id, int(time.time())
        )
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


_USER_ID_RE = re.compile(r"User ID:\s*(\d{15,21})")
_MENTION_RE = re.compile(r"<@!?(\d{15,21})>")
_CHANNEL_MENTION_RE = re.compile(r"<#(\d{15,21})>")


def _parse_circle_embed(
    embed: discord.Embed, message: discord.Message, vc_name_to_id: dict[str, int]
) -> tuple[int, int, int, str] | None:
    """One CircleBot log embed -> (ts_utc, user_id, channel_id, kind), or None
    for entries that aren't voice joins/leaves (nick changes, server joins…)."""
    text = " ".join(filter(None, (embed.title, embed.description)))
    if "has joined a voice channel" in text:
        kind = "join"
    elif "has left a voice channel" in text:
        kind = "leave"
    else:
        return None

    match = _USER_ID_RE.search(embed.footer.text or "") or _MENTION_RE.search(text)
    if not match:
        return None
    user_id = int(match.group(1))

    channel_id = 0  # unknown/renamed channels still count toward totals
    for field in embed.fields:
        if (field.name or "").strip().lower() == "channel":
            value = (field.value or "").strip()
            chan_match = _CHANNEL_MENTION_RE.search(value)
            channel_id = int(chan_match.group(1)) if chan_match else vc_name_to_id.get(value, 0)
            break

    when = embed.timestamp or message.created_at
    return (int(when.timestamp()), user_id, channel_id, kind)


@backlog_group.command(
    name="vc",
    description="Rebuild past voice sessions from CircleBot's log history",
)
@app_commands.describe(channel="The channel where CircleBot posts its logs")
async def backlog_vc(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    guild = interaction.guild
    if guild.id in _backfills_running:
        await interaction.response.send_message(
            "A backfill is already running for this server.", ephemeral=True
        )
        return
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.read_message_history):
        await interaction.response.send_message(
            f"I can't read the history of {channel.mention} — grant me "
            "**View Channel** and **Read Message History** there first.",
            ephemeral=True,
        )
        return
    await interaction.response.defer()
    _backfills_running.add(guild.id)
    try:
        await _run_vc_backfill(interaction, channel)
    finally:
        _backfills_running.discard(guild.id)


async def _run_vc_backfill(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    guild = interaction.guild
    progress = _progress_editor(interaction)
    vc_name_to_id = {
        ch.name: ch.id for ch in (*guild.voice_channels, *guild.stage_channels)
    }

    events: list[tuple[int, int, int, str]] = []
    scanned = embeds_seen = 0
    async for msg in channel.history(limit=None, oldest_first=True):
        if msg.author.id != config.CIRCLEBOT_ID:
            continue
        scanned += 1
        for embed in msg.embeds:
            embeds_seen += 1
            event = _parse_circle_embed(embed, msg, vc_name_to_id)
            if event is not None:
                events.append(event)
        if scanned % 500 == 0:
            await progress(
                f"Reading CircleBot logs in {channel.mention}… {scanned:,} messages "
                f"scanned, {len(events):,} voice events found."
            )

    if scanned == 0:
        await progress(
            f"No CircleBot messages found in {channel.mention} — is that the right "
            "log channel?", force=True,
        )
        return
    if embeds_seen == 0:
        await progress(
            f"Found {scanned:,} CircleBot messages but Discord returned no embed "
            "data. Enable **Message Content Intent** in the Developer Portal "
            "(Bot → Privileged Gateway Intents) and restart Iris.", force=True,
        )
        return

    # NEVER log bots; respect opt-outs even for historical data.
    def tracked(user_id: int) -> bool:
        if user_id in client.opted_out:
            return False
        member = guild.get_member(user_id)
        return member is None or not member.bot

    events = [e for e in events if tracked(e[1])]

    cutoff = await storage.earliest_live_voice_start(guild.id)
    sessions, ignored = analysis.reconstruct_sessions(events, cutoff)
    replaced = await storage.delete_voice_sessions_by_source(guild.id, "backlog")
    added = await storage.add_voice_sessions_bulk(guild.id, sessions)

    users = len({s[0] for s in sessions})
    lines = [
        f"Voice backlog complete: rebuilt **{added:,}** sessions for {users} members "
        f"from {len(events):,} join/leave events."
    ]
    if replaced:
        lines.append(f"Replaced {replaced:,} sessions from a previous import.")
    if cutoff:
        lines.append("Events after Iris's own voice tracking began were skipped "
                     "so nothing is counted twice.")
    if ignored:
        lines.append(f"{ignored:,} events couldn't be paired (log gaps) and were ignored.")
    lines.append("Bots and opted-out members were excluded.")
    await progress("\n".join(lines), force=True)


client.tree.add_command(backlog_group)


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
                "Discord refused the connection: enable BOTH 'Server Members "
                "Intent' and 'Message Content Intent' in the Developer Portal "
                "(your app -> Bot -> Privileged Gateway Intents), then restart."
            )
        finally:
            # Graceful shutdown: everyone in VC "leaves" now, so no session is
            # left open and reconcile has nothing to guess at next boot.
            heartbeat_loop.cancel()
            await storage.close_all_open_sessions(int(time.time()))
            await storage.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

import os
import re
import discord
from discord.ext import commands, tasks
from discord import app_commands, Interaction
from dotenv import load_dotenv
import asyncio
import sys
import calendar
from datetime import datetime, timedelta, timezone

# Load environment variables from .env file
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Set event loop policy for Windows
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Set up bot intents and command prefix
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True  # Required for reading messages
bot = commands.Bot(command_prefix="!", intents=intents)

# Voice channel config
config_role_id = None
user_vc_map = {}  # {user_id: channel_id}
sticky_data = {}

# Timezone config: {user_id: UTC offset in hours}
user_timezones = {}

# --- Timezone Labels ---
TIMEZONE_LABELS = {
    -12: "UTC-12",
    -11: "UTC-11",
    -10: "HST",
    -9: "AKST",
    -8: "PST",
    -7: "MST",
    -6: "CST",
    -5: "EST",
    -4: "AST",
    -3: "BRT",
    -2: "UTC-2",
    -1: "UTC-1",
    0: "GMT",
    1: "CET",
    2: "EET",
    3: "MSK",
    4: "GST",
    5: "PKT",
    5.5: "IST",
    6: "BST",
    7: "ICT",
    8: "SGT",
    9: "JST",
    10: "AEST",
    11: "UTC+11",
    12: "UTC+12",
}


def get_tz_label(offset: float) -> str:
    return TIMEZONE_LABELS.get(
        offset,
        f"UTC{'+' if offset >= 0 else ''}{int(offset) if offset == int(offset) else offset}",
    )


# --- Time Detection ---

TIME_PATTERN = re.compile(
    r"\b(\d{1,2}):(\d{2})\s*(am|pm|AM|PM)\b"
    r"|\b(\d{1,2})\s*(am|pm|AM|PM)\b"
    r"|\b([01]?\d|2[0-3]):([0-5]\d)\b"
)


def parse_times_from_text(text: str) -> list[tuple[int, int, int]]:
    """Extract times, return list of (hour, minute, unix_ts) tuples."""
    now = datetime.now(timezone.utc)
    results = []
    seen = set()

    for match in TIME_PATTERN.finditer(text):
        hour = None
        minute = 0

        if match.group(1) is not None:
            hour = int(match.group(1))
            minute = int(match.group(2))
            period = match.group(3).lower()
            if period == "pm" and hour != 12:
                hour += 12
            elif period == "am" and hour == 12:
                hour = 0
        elif match.group(4) is not None:
            hour = int(match.group(4))
            period = match.group(5).lower()
            if period == "pm" and hour != 12:
                hour += 12
            elif period == "am" and hour == 12:
                hour = 0
        elif match.group(6) is not None:
            hour = int(match.group(6))
            minute = int(match.group(7))

        if hour is not None and 0 <= hour <= 23 and 0 <= minute <= 59:
            key = (hour, minute)
            if key not in seen:
                seen.add(key)
                dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if dt < now:
                    dt += timedelta(days=1)
                results.append((hour, minute, int(dt.timestamp())))

    return results


def format_12h(hour: int, minute: int) -> str:
    """Format hour/minute as 12h string like '5:00 PM'."""
    period = "AM" if hour < 12 else "PM"
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12
    if minute == 0:
        return f"{display_hour} {period}"
    return f"{display_hour}:{minute:02d} {period}"


# --- Dropdown Classes ---


class RoleSelect(discord.ui.Select):
    def __init__(self, roles: list[discord.Role]):
        options = [
            discord.SelectOption(label=role.name, value=str(role.id))
            for role in roles
            if not role.managed and role.name != "@everyone"
        ]
        super().__init__(
            placeholder="Select a role for voice creation",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        global config_role_id
        config_role_id = int(self.values[0])
        await interaction.response.send_message(
            f"Setup complete! Role '{interaction.guild.get_role(config_role_id).name}' can create VCs.",
            ephemeral=True,
        )


class RoleSetupView(discord.ui.View):
    def __init__(self, roles: list[discord.Role]):
        super().__init__(timeout=None)
        self.add_item(RoleSelect(roles))


class TimezoneSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="UTC-12 (Baker Island)", value="-12"),
            discord.SelectOption(label="UTC-11 (Samoa)", value="-11"),
            discord.SelectOption(label="UTC-10 (Hawaii)", value="-10"),
            discord.SelectOption(label="UTC-9 (Alaska)", value="-9"),
            discord.SelectOption(label="UTC-8 (PST)", value="-8"),
            discord.SelectOption(label="UTC-7 (MST)", value="-7"),
            discord.SelectOption(label="UTC-6 (CST)", value="-6"),
            discord.SelectOption(label="UTC-5 (EST)", value="-5"),
            discord.SelectOption(label="UTC-4 (AST)", value="-4"),
            discord.SelectOption(label="UTC-3 (BRT)", value="-3"),
            discord.SelectOption(label="UTC-2", value="-2"),
            discord.SelectOption(label="UTC-1 (Azores)", value="-1"),
            discord.SelectOption(label="UTC+0 (GMT/UTC)", value="0"),
            discord.SelectOption(label="UTC+1 (CET)", value="1"),
            discord.SelectOption(label="UTC+2 (EET)", value="2"),
            discord.SelectOption(label="UTC+3 (MSK)", value="3"),
            discord.SelectOption(label="UTC+4 (GST)", value="4"),
            discord.SelectOption(label="UTC+5 (PKT)", value="5"),
            discord.SelectOption(label="UTC+5:30 (IST)", value="5.5"),
            discord.SelectOption(label="UTC+6 (BST)", value="6"),
            discord.SelectOption(label="UTC+7 (ICT)", value="7"),
            discord.SelectOption(label="UTC+8 (SGT/CST)", value="8"),
            discord.SelectOption(label="UTC+9 (JST/KST)", value="9"),
            discord.SelectOption(label="UTC+10 (AEST)", value="10"),
        ]
        super().__init__(
            placeholder="Select your timezone",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        offset = float(self.values[0])
        user_timezones[interaction.user.id] = offset
        label = get_tz_label(offset)
        await interaction.response.send_message(
            f"Your timezone is set to **{label}**. Times you mention will be converted for everyone!",
            ephemeral=True,
        )


class TimezoneSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TimezoneSelect())


# --- /time Command Views ---


class MonthSelect(discord.ui.Select):
    def __init__(self):
        now = datetime.now(timezone.utc)
        options = []
        seen = set()
        for i in range(12):
            dt = now + timedelta(days=30 * i)
            month_name = calendar.month_name[dt.month]
            label = f"{month_name} {dt.year}"
            value = f"{dt.year}-{dt.month:02d}"
            if value not in seen:
                seen.add(value)
                options.append(discord.SelectOption(label=label, value=value))
        super().__init__(
            placeholder="Select month", min_values=1, max_values=1, options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_month = self.values[0]
        year, month = map(int, self.values[0].split("-"))
        max_day = calendar.monthrange(year, month)[1]

        # Update day select options
        day_select: DaySelect = self.view.day_select
        day_select.options = [
            discord.SelectOption(label=str(d), value=str(d))
            for d in range(1, max_day + 1)
        ]
        day_select.disabled = False
        await interaction.response.edit_message(view=self.view)


class DaySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=str(d), value=str(d)) for d in range(1, 32)
        ]
        super().__init__(
            placeholder="Select day",
            min_values=1,
            max_values=1,
            options=options,
            disabled=False,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_day = int(self.values[0])
        await interaction.response.edit_message(view=self.view)


class HourSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for h in range(24):
            period = "AM" if h < 12 else "PM"
            display = h % 12
            if display == 0:
                display = 12
            label = f"{display} {period} ({h:02d}:00)"
            options.append(discord.SelectOption(label=label, value=str(h)))
        super().__init__(
            placeholder="Select hour",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_hour = int(self.values[0])
        await interaction.response.edit_message(view=self.view)


class MinuteSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=f":{m:02d}", value=str(m))
            for m in range(0, 60, 5)
        ]
        super().__init__(
            placeholder="Select minute",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_minute = int(self.values[0])
        await interaction.response.edit_message(view=self.view)


class SendButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Send in Chat", style=discord.ButtonStyle.green, row=4)

    async def callback(self, interaction: discord.Interaction):
        view: TimePickerView = self.view
        ts = view.build_timestamp()
        if ts is None:
            return await interaction.response.send_message(
                "Please select all fields first.", ephemeral=True
            )

        offset = user_timezones.get(interaction.user.id, 0)
        tz_label = get_tz_label(offset)
        local_str = format_12h(view.selected_hour, view.selected_minute)

        await interaction.response.send_message(
            f"🕐 {local_str} {tz_label} → <t:{ts}:t>",
        )


class CopyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Copy Code", style=discord.ButtonStyle.grey, row=4)

    async def callback(self, interaction: discord.Interaction):
        view: TimePickerView = self.view
        ts = view.build_timestamp()
        if ts is None:
            return await interaction.response.send_message(
                "Please select all fields first.", ephemeral=True
            )

        await interaction.response.send_message(
            f"Copy this into any message:\n```\n<t:{ts}:t>\n```\nWith full date + time:\n```\n<t:{ts}:f>\n```",
            ephemeral=True,
        )


class TimePickerView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.selected_month = None
        self.selected_day = None
        self.selected_hour = None
        self.selected_minute = None

        self.month_select = MonthSelect()
        self.day_select = DaySelect()
        self.hour_select = HourSelect()
        self.minute_select = MinuteSelect()

        self.add_item(self.month_select)
        self.add_item(self.day_select)
        self.add_item(self.hour_select)
        self.add_item(self.minute_select)
        self.add_item(SendButton())
        self.add_item(CopyButton())

    def build_timestamp(self) -> int | None:
        if (
            self.selected_month is None
            or self.selected_day is None
            or self.selected_hour is None
            or self.selected_minute is None
        ):
            return None

        year, month = map(int, self.selected_month.split("-"))
        offset = user_timezones.get(self.user_id, 0)

        dt = datetime(
            year,
            month,
            self.selected_day,
            self.selected_hour,
            self.selected_minute,
            0,
            tzinfo=timezone.utc,
        )
        # Convert from user's local to UTC
        utc_ts = int(dt.timestamp() - (offset * 3600))
        return utc_ts


# --- Events ---


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    cleanup_empty_vcs.start()
    repost_stickies.start()
    try:
        synced = await bot.tree.sync()
        print(f"Slash commands synced: {len(synced)}")
    except Exception as e:
        print(e)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    results = parse_times_from_text(message.content)
    if not results:
        await bot.process_commands(message)
        return

    offset_hours = user_timezones.get(message.author.id, 0)
    tz_label = get_tz_label(offset_hours)

    lines = []
    for hour, minute, ts in results:
        adjusted_ts = int(ts - (offset_hours * 3600))
        local_str = format_12h(hour, minute)
        lines.append(f"🕐 {local_str} {tz_label} → <t:{adjusted_ts}:t>")

    reply = "\n".join(lines)
    await message.reply(reply, mention_author=False)
    await bot.process_commands(message)


# --- Slash Commands ---


@bot.tree.command(name="help", description="Show all bot commands and features.")
async def help_command(interaction: Interaction):
    embed = discord.Embed(title="Bot Commands", color=0x2ECC71)
    embed.add_field(
        name="🎙️ Voice Channels",
        value=(
            "`/setup` - Set which role can create VCs\n"
            "`/vc` - Create your own voice channel (max 1/user)\n"
            "`/vcend` - Delete your voice channel"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛡️ Moderation",
        value=(
            "`/purge` - Bulk delete messages (Admin)\n"
            "`/vcendall` - Delete all custom VCs (Admin)\n"
            "`/sticky` - Set or remove a sticky message\n"
            "`/stickyembed` - Set a sticky embed with title, color & image"
        ),
        inline=False,
    )
    embed.add_field(
        name="🕐 Time Zones",
        value=(
            "`/timezone` - Set your timezone\n"
            "`/time` - Pick a date & time, send or copy the timestamp\n"
            "Mention a time in chat (e.g. `5pm`, `17:00`, `2:30pm`) "
            "and the bot converts it for everyone!"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Auto",
        value="Empty VCs are cleaned every 5 mins\nStickies reposted to stay visible",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="timezone",
    description="Set your timezone so the bot can convert times you mention.",
)
async def timezone_command(interaction: Interaction):
    view = TimezoneSetupView()
    embed = discord.Embed(
        title="Set Your Timezone",
        description="Pick your timezone below. When you mention a time in chat, the bot will convert it for everyone using Discord timestamps.",
        color=0x3498DB,
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(
    name="time",
    description="Pick a date and time, then send or copy the Discord timestamp.",
)
async def time_command(interaction: Interaction):
    view = TimePickerView(interaction.user.id)
    embed = discord.Embed(
        title="Time Picker",
        description="Select a month, day, hour, and minute below.\nThen **Send in Chat** for everyone or **Copy Code** to paste it yourself.",
        color=0x3498DB,
    )
    offset = user_timezones.get(interaction.user.id, 0)
    tz_label = get_tz_label(offset)
    embed.set_footer(text=f"Your timezone: {tz_label} (change with /timezone)")
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# Voice Channel Commands
@bot.tree.command(
    name="setup", description="Choose which role can create voice channels."
)
async def setup_command(interaction: Interaction):
    roles = interaction.guild.roles
    view = RoleSetupView(roles)
    embed = discord.Embed(
        title="Bot Setup", description="Pick the role that can create VCs."
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="vc", description="Create your custom voice channel.")
@app_commands.describe(name="Name of your new VC")
async def create_vc(interaction: Interaction, name: str):
    if config_role_id is not None:
        role = interaction.guild.get_role(config_role_id)
        if not role or role not in interaction.user.roles:
            return await interaction.response.send_message(
                "You don't have permission.", ephemeral=True
            )

    if user_vc_map.get(interaction.user.id):
        return await interaction.response.send_message(
            "You already have a VC!", ephemeral=True
        )

    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True
        ),
        interaction.user: discord.PermissionOverwrite(
            manage_channels=True, connect=True, speak=True
        ),
    }
    category = interaction.channel.category if interaction.channel else None
    vc = await interaction.guild.create_voice_channel(
        f"!{name}", category=category, overwrites=overwrites
    )
    user_vc_map[interaction.user.id] = vc.id

    await interaction.response.send_message(f"Created VC: {vc.mention}", ephemeral=True)

    embed = discord.Embed(
        title="Voice Channel Created",
        description=f"{interaction.user.mention} created {vc.mention}",
        color=0x2ECC71,
    )
    await interaction.channel.send(embed=embed)


@bot.tree.command(name="vcend", description="End your custom voice channel.")
async def end_vc(interaction: Interaction):
    vc_id = user_vc_map.get(interaction.user.id)
    if not vc_id:
        return await interaction.response.send_message(
            "You have no VC.", ephemeral=True
        )

    vc = interaction.guild.get_channel(vc_id)
    if vc:
        await vc.delete()
    del user_vc_map[interaction.user.id]
    await interaction.response.send_message("Your VC has been deleted.", ephemeral=True)


@bot.tree.command(name="vcendall", description="Admin-only: Delete all custom VCs.")
async def vcendall(interaction: Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admin only.", ephemeral=True)

    for uid, cid in list(user_vc_map.items()):
        ch = interaction.guild.get_channel(cid)
        if ch:
            await ch.delete()
        del user_vc_map[uid]
    await interaction.response.send_message("All custom VCs deleted.", ephemeral=True)


# Sticky Commands
@bot.tree.command(
    name="sticky", description="Admin-only: Set or remove a sticky text in the channel."
)
@app_commands.describe(
    text="Text for the sticky. Leave blank to remove any existing sticky."
)
async def sticky_command(interaction: Interaction, text: str = None):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admin only.", ephemeral=True)

    channel_id = interaction.channel_id
    if text is None or text.strip() == "":
        if channel_id in sticky_data:
            data = sticky_data.pop(channel_id)
            if data.get("last_id"):
                try:
                    old_msg = await interaction.channel.fetch_message(data["last_id"])
                    await old_msg.delete()
                except discord.NotFound:
                    pass
            await interaction.response.send_message(
                "Sticky removed for this channel.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "No sticky to remove.", ephemeral=True
            )
        return

    sticky_data[channel_id] = {"type": "text", "content": text, "last_id": None}
    msg = await interaction.channel.send(text)
    sticky_data[channel_id]["last_id"] = msg.id
    await interaction.response.send_message("Sticky text set!", ephemeral=True)


@bot.tree.command(
    name="stickyembed",
    description="Admin-only: Make a sticky embed that reappears every 5 mins.",
)
@app_commands.describe(
    title="Embed title",
    message="Main text in the embed",
    color="Optional hex color (#RRGGBB)",
    image_url="Optional image URL",
)
async def stickyembed_command(
    interaction: Interaction,
    title: str,
    message: str,
    color: str = None,
    image_url: str = None,
):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admin only.", ephemeral=True)

    channel_id = interaction.channel_id
    color_int = 0x2ECC71
    if color:
        try:
            color_int = int(color.replace("#", ""), 16)
        except ValueError:
            pass

    embed = discord.Embed(title=title, description=message, color=color_int)
    if image_url:
        embed.set_image(url=image_url)
    msg = await interaction.channel.send(embed=embed)

    sticky_data[channel_id] = {
        "type": "embed",
        "title": title,
        "message": message,
        "color": color_int,
        "image_url": image_url,
        "last_id": msg.id,
    }
    await interaction.response.send_message(
        "Sticky embed set for this channel!", ephemeral=True
    )


# Background Tasks
@tasks.loop(minutes=5)
async def cleanup_empty_vcs():
    for uid, cid in list(user_vc_map.items()):
        channel = bot.get_channel(cid)
        if channel and len(channel.members) == 0:
            await channel.delete()
            del user_vc_map[uid]


@tasks.loop(minutes=5)
async def repost_stickies():
    for channel_id, data in sticky_data.items():
        channel = bot.get_channel(channel_id)
        if not channel:
            continue
        if channel.last_message_id != data["last_id"]:
            if data["last_id"]:
                try:
                    old_msg = await channel.fetch_message(data["last_id"])
                    await old_msg.delete()
                except discord.NotFound:
                    pass
            if data["type"] == "text":
                msg = await channel.send(data["content"])
                data["last_id"] = msg.id
            elif data["type"] == "embed":
                embed = discord.Embed(
                    title=data["title"],
                    description=data["message"],
                    color=data["color"],
                )
                if data["image_url"]:
                    embed.set_image(url=data["image_url"])
                msg = await channel.send(embed=embed)
                data["last_id"] = msg.id


@bot.tree.command(
    name="purge", description="Admin-only: Purge a number of messages from the channel."
)
@app_commands.describe(number="Number of messages to purge")
async def purge_command(interaction: Interaction, number: int):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admin only.", ephemeral=True)
    if number <= 0:
        return await interaction.response.send_message(
            "Please specify a positive number of messages to purge.", ephemeral=True
        )
    deleted = await interaction.channel.purge(limit=number)
    await interaction.response.send_message(
        f"Purged {len(deleted)} messages.", ephemeral=True
    )


# Run the bot
bot.run(TOKEN)

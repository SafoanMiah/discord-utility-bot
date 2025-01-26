import os
import json
import discord
from discord.ext import commands, tasks
from discord import app_commands, Interaction
from dotenv import load_dotenv
import asyncio
import sys

# --- Load environment variables from .env file ---
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# --- Set event loop policy for Windows ---
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# --- JSON config helpers ---
CONFIG_FILE = "config.json"

def load_config():
    if os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {"guilds": {}}

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=4)

# This dictionary maps guild_id -> role_id.
# It’s loaded/saved from JSON so we remember across restarts.
bot_config = load_config()
role_map = bot_config["guilds"]  # Shortcut

# --- Set up bot intents & command prefix ---
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- In-memory tracking ---
user_vc_map = {}  # {user_id: channel_id}
sticky_data = {}

# --- Dropdown for voice-creation role ---
class RoleSelect(discord.ui.Select):
    def __init__(self, roles: list[discord.Role]):
        options = [
            discord.SelectOption(label=role.name, value=str(role.id))
            for role in roles
            if not role.managed and role.name != "@everyone"
        ]
        super().__init__(placeholder="Select a role for voice creation",
                         min_values=1, max_values=1,
                         options=options)

    async def callback(self, interaction: discord.Interaction):
        # Save the chosen role in role_map for this guild
        guild_id = str(interaction.guild_id)
        role_map[guild_id] = self.values[0]  # role_id as string

        # Save updated config
        bot_config["guilds"] = role_map
        save_config(bot_config)

        chosen_role = interaction.guild.get_role(int(self.values[0]))
        await interaction.response.send_message(
            f"Setup complete! Role '{chosen_role.name}' can create VCs.",
            ephemeral=True
        )

# --- View for role setup ---
class RoleSetupView(discord.ui.View):
    def __init__(self, roles: list[discord.Role]):
        super().__init__(timeout=None)
        self.add_item(RoleSelect(roles))

# --- Events ---
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    # Start background tasks
    cleanup_empty_vcs.start()
    repost_stickies.start()

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"Slash commands synced: {len(synced)}")
    except Exception as e:
        print(e)

# --- Setup Command ---
@bot.tree.command(name="setup", description="Choose which role can create voice channels.")
async def setup_command(interaction: Interaction):
    roles = interaction.guild.roles
    view = RoleSetupView(roles)
    embed = discord.Embed(
        title="Bot Setup",
        description="Pick the role that can create VCs."
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# --- Create VC ---
@bot.tree.command(name="vc", description="Create your custom voice channel.")
@app_commands.describe(name="Name of your new VC")
async def create_vc(interaction: Interaction, name: str):
    guild_id = str(interaction.guild_id)
    # Check if role is set for this guild
    if guild_id not in role_map:
        return await interaction.response.send_message("Bot not set up yet. Use /setup.", ephemeral=True)

    role_id = int(role_map[guild_id])
    role = interaction.guild.get_role(role_id)
    if not role or role not in interaction.user.roles:
        return await interaction.response.send_message("You don't have permission.", ephemeral=True)

    if user_vc_map.get(interaction.user.id):
        return await interaction.response.send_message("You already have a VC!", ephemeral=True)

    # Give the creator manage_channels permission
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True),
        interaction.user: discord.PermissionOverwrite(manage_channels=True, connect=True, speak=True)
    }
    category = interaction.channel.category if interaction.channel else None
    vc = await interaction.guild.create_voice_channel(f'!{name}', category=category, overwrites=overwrites)
    user_vc_map[interaction.user.id] = vc.id

    await interaction.response.send_message(f"Created VC: {vc.mention}", ephemeral=True)

# --- End VC ---
@bot.tree.command(name="vcend", description="End your custom voice channel.")
async def end_vc(interaction: Interaction):
    vc_id = user_vc_map.get(interaction.user.id)
    if not vc_id:
        return await interaction.response.send_message("You have no VC.", ephemeral=True)

    vc = interaction.guild.get_channel(vc_id)
    if vc:
        await vc.delete()
    del user_vc_map[interaction.user.id]
    await interaction.response.send_message("Your VC has been deleted.", ephemeral=True)

# --- End All VCs ---
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

# --- Sticky Commands ---
@bot.tree.command(name="sticky", description="Admin-only: Set or remove a sticky text in the channel.")
@app_commands.describe(text="Text for the sticky. Leave blank to remove any existing sticky.")
async def sticky_command(interaction: Interaction, text: str = None):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admin only.", ephemeral=True)

    channel_id = interaction.channel_id

    # If no text given, remove sticky
    if not text or text.strip() == "":
        if channel_id in sticky_data:
            data = sticky_data.pop(channel_id)
            if data.get("last_id"):
                try:
                    old_msg = await interaction.channel.fetch_message(data["last_id"])
                    await old_msg.delete()
                except discord.NotFound:
                    pass
            return await interaction.response.send_message("Sticky removed.", ephemeral=True)
        else:
            return await interaction.response.send_message("No sticky found.", ephemeral=True)

    # Otherwise, set a new sticky text
    sticky_data[channel_id] = {
        "type": "text",
        "content": text,
        "last_id": None
    }
    msg = await interaction.channel.send(text)
    sticky_data[channel_id]["last_id"] = msg.id
    await interaction.response.send_message("Sticky text set!", ephemeral=True)

@bot.tree.command(name="stickyembed", description="Admin-only: Make a sticky embed that reappears every 5 mins.")
@app_commands.describe(
    title="Embed title",
    message="Main text in the embed",
    color="Optional hex color (#RRGGBB)",
    image_url="Optional image URL"
)
async def stickyembed_command(
    interaction: Interaction,
    title: str,
    message: str,
    color: str = None,
    image_url: str = None
):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admin only.", ephemeral=True)

    channel_id = interaction.channel_id
    color_int = 0x2ecc71
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
        "last_id": msg.id
    }
    await interaction.response.send_message("Sticky embed set!", ephemeral=True)

# --- Background Tasks ---
@tasks.loop(minutes=5)
async def cleanup_empty_vcs():
    """Deletes voice channels if they're empty."""
    for uid, cid in list(user_vc_map.items()):
        channel = bot.get_channel(cid)
        if channel and len(channel.members) == 0:
            await channel.delete()
            del user_vc_map[uid]

@tasks.loop(minutes=5)
async def repost_stickies():
    """Checks if stickies are last in their channel; if not, re-posts them."""
    for channel_id, data in sticky_data.items():
        channel = bot.get_channel(channel_id)
        if not channel:
            continue

        # If the last message is not the sticky, delete and re-post
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
                    color=data["color"]
                )
                if data["image_url"]:
                    embed.set_image(url=data["image_url"])
                msg = await channel.send(embed=embed)
                data["last_id"] = msg.id

@bot.tree.command(name="purge", description="Admin-only: Purge a number of messages from the channel.")
@app_commands.describe(number="Number of messages to purge")
async def purge_command(interaction: Interaction, number: int):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admin only.", ephemeral=True)

    if number <= 0:
        return await interaction.response.send_message("Please specify a positive number.", ephemeral=True)

    deleted = await interaction.channel.purge(limit=number)
    await interaction.response.send_message(f"Purged {len(deleted)} messages.", ephemeral=True)

# --- Run the bot ---
bot.run(TOKEN)

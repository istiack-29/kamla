"""
KAMLABot — Main Entry Point
Loads all Cogs and starts the bot. Bot token and application ID are pulled
from environment variables (DISCORD_TOKEN, APPLICATION_ID).
"""

import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from keep_alive import keep_alive

load_dotenv()

TOKEN = os.environ.get("DISCORD_TOKEN")
APPLICATION_ID = int(os.environ.get("APPLICATION_ID", 0))

# ── Intents ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True          # Required: track member joins and role changes
intents.message_content = True  # Required: read message content for timer regex
intents.guilds = True
intents.voice_states = True     # Required: GA and room voice tracking

# ── Bot ───────────────────────────────────────────────────────────────────────
bot = commands.Bot(
    command_prefix="!",          # Fallback prefix (slash commands are primary)
    intents=intents,
    application_id=APPLICATION_ID,
)

# ── Shared in-memory state ────────────────────────────────────────────────────
# All state lives here — no database. This dict is the single source of truth
# for runtime data. Channel/message objects on Discord are the persistent store.
bot.state = {
    # Set during the setup wizard
    "tournament_type": None,   # 1 = Regular, 2 = Fundraiser
    "format": None,            # 1 = AP,      2 = BP
    "num_rooms": 0,
    "server_name": "",

    # Role IDs (populated after /startb)
    "roles": {},               # {"ORG": id, "CAP": id, ...}

    # Channel IDs (populated during base build)
    "channels": {},            # {"assign": id, "motion": id, ...}

    # GA voice session tracking: {user_id: {"join": datetime, "exit": datetime|None}}
    "ga_sessions": {},

    # Room voice tracking: {category_id: {user_id: {"join": datetime, "exit": datetime|None}}}
    "room_sessions": {},

    # Active visual timers: {message_id: asyncio.Task}
    "timers": {},
}


# ── Cog loader ────────────────────────────────────────────────────────────────
COGS = [
    "logger",        # Must load first — audit log channel used by others
    "setup_nuke",
    "base_builder",
    "room_engine",
    "commands",
    "visual_timer",
]


@bot.event
async def on_ready():
    print(f"[KAMLABot] Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"[KAMLABot] Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"[KAMLABot] Failed to sync commands: {e}")


@bot.event
async def on_member_join(member: discord.Member):
    """Tag new joins in #welcome and direct them to get-role."""
    welcome_id = bot.state["channels"].get("welcome")
    if not welcome_id:
        return
    channel = member.guild.get_channel(welcome_id)
    if channel:
        embed = discord.Embed(
            title="Welcome!",
            description=(
                f"Welcome to **{member.guild.name}**, {member.mention}! 👋\n\n"
                "Head over to <#" + str(bot.state["channels"].get("get_role", 0)) + "> "
                "to grab your role and get started."
            ),
            color=discord.Color.green(),
        )
        await channel.send(embed=embed)


async def main():
    keep_alive()
    async with bot:
        for cog in COGS:
            try:
                await bot.load_extension(cog)
                print(f"[KAMLABot] Loaded cog: {cog}")
            except Exception as e:
                print(f"[KAMLABot] Failed to load cog {cog}: {e}")
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

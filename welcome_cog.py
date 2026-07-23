"""
welcome_cog.py — Beautiful member welcome messages for KAMLA.

Sends a rich embed to the 👋🏻︱welcome channel whenever a new member joins
the server. The embed shows the member's avatar, a personalised greeting,
role instructions, and a quick-start channel guide.
"""

from datetime import datetime, timezone

import discord
from discord.ext import commands

WELCOME_CHANNEL_NAME = "👋🏻︱welcome"

# Accent colour — deep indigo, feels premium without being harsh
WELCOME_COLOR = discord.Color.from_rgb(88, 86, 214)


def _ordinal(n: int) -> str:
    """Return e.g. '1st', '2nd', '42nd' member label."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _build_welcome_embed(member: discord.Member) -> discord.Embed:
    guild = member.guild
    now = datetime.now(timezone.utc)
    member_count = guild.member_count or 1

    embed = discord.Embed(
        title=f"Welcome to {guild.name}! 🎉",
        description=(
            f"Hey {member.mention}, glad you're here!\n"
            f"You are our **{_ordinal(member_count)} member**.\n\n"
            "This server is powered by **KAMLA** — an automated debate tournament manager. "
            "Everything is organised for you; just follow the steps below to get started."
        ),
        color=WELCOME_COLOR,
        timestamp=now,
    )

    # Large avatar at the top
    embed.set_thumbnail(url=member.display_avatar.url)

    # Server banner / icon as a small image if available
    if guild.banner:
        embed.set_image(url=guild.banner.url)
    elif guild.icon:
        embed.set_author(name=guild.name, icon_url=guild.icon.url)

    # ── Step-by-step quick start ────────────────────────────────────────────
    embed.add_field(
        name="🚀  Quick Start",
        value=(
            "1️⃣  Go to **❓︱get-role** and click your role button\n"
            "2️⃣  Read **📌︱information** for tournament rules\n"
            "3️⃣  Join your assigned **ROOM** when rounds start"
        ),
        inline=False,
    )

    # ── Role guide ──────────────────────────────────────────────────────────
    embed.add_field(
        name="🎭  Roles",
        value=(
            "🔵 **Invited Adjudicator** — Officially invited judges\n"
            "🟣 **Independent Adjudicator** — Self-registered judges\n"
            "🩵 **Debater** — Competing speakers\n"
            "⬜ **Visitor** — Observers & guests"
        ),
        inline=True,
    )

    # ── Key channels ────────────────────────────────────────────────────────
    embed.add_field(
        name="📡  Key Channels",
        value=(
            "**❓︱get-role** — Claim your role\n"
            "**📌︱information** — Rules & docs\n"
            "**🎙️︱grand-auditorium** — Main stage\n"
            "**⏱️︱timer** — Speech countdown"
        ),
        inline=True,
    )

    embed.set_footer(
        text=f"KAMLA  •  Joined {now.strftime('%d %b %Y')}",
        icon_url=guild.me.display_avatar.url,
    )

    return embed


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Send a beautiful welcome embed to the welcome channel."""
        guild = member.guild
        channel = discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL_NAME)
        if channel is None:
            # Fall back to system channel if welcome channel doesn't exist
            channel = guild.system_channel
        if channel is None:
            return

        embed = _build_welcome_embed(member)

        # Mention the member in the message body so they get a notification,
        # but keep the embed clean — the mention sits above the embed.
        try:
            await channel.send(
                content=f"**{member.mention}** just joined the server! 👋",
                embed=embed,
            )
        except discord.Forbidden:
            pass



async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))

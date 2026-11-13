"""
welcome_cog.py — Beautiful member welcome messages for KAMLA.

Sends a short welcome embed to the 👋🏻︱welcome channel whenever a new member joins
the server: the member's avatar, a personalised greeting and their member number.
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
    """Short on purpose: a greeting and the member number. Role / channel guidance lives in the
    get-role and information channels instead of being repeated for every new member."""
    guild = member.guild
    now = datetime.now(timezone.utc)
    member_count = guild.member_count or 1

    embed = discord.Embed(
        title=f"Welcome to {guild.name}! 🎉",
        description=(
            f"Hey {member.mention}, glad you're here!\n"
            f"You are our **{_ordinal(member_count)} member**."
        ),
        color=WELCOME_COLOR,
        timestamp=now,
    )

    # Member avatar
    embed.set_thumbnail(url=member.display_avatar.url)

    # Server banner / icon if available
    if guild.banner:
        embed.set_image(url=guild.banner.url)
    elif guild.icon:
        embed.set_author(name=guild.name, icon_url=guild.icon.url)

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
        # Nobody needs a welcome for the developer link: whoever joins through it (the developer) is not a
        # participant. The invite tracker tells us which link was used (it answers within a couple of seconds).
        try:
            from invite_roles_cog import join_outcome, DEV_KEY
            if await join_outcome(member) == DEV_KEY:
                return
        except Exception:  # noqa: BLE001 - never let this block a normal welcome
            pass
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

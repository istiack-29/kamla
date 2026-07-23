"""
toss_cog.py — Coin toss feature for KAMLA.

• "🪙︱toss-coin"  — text command `toss` (any letter case), restricted to
  CAP / TABBY / ORG. Everyone else may view the channel but not use it.
• "📨︱ga-text"    — `/toss` slash command, available to every non-visitor
  role holder.

No database is used; results are ephemeral and not stored anywhere.
"""

import asyncio
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
TOSS_CHANNEL_NAME = "🪙︱toss-coin"
GA_TEXT_CHANNEL_NAME = "📨︱ga-text"
TOSS_ALLOWED_ROLES = {"CAP", "TABBY", "ORG"}
NON_VISITOR_ROLES = {
    "ORG", "CAP", "TABBY", "EQUITY",
    "INVITED ADJUDICATOR", "INDEPENDENT ADJUDICATOR", "DEBATER",
}


def _can_toss(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.name in TOSS_ALLOWED_ROLES for r in member.roles)


def _is_non_visitor(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.name in NON_VISITOR_ROLES for r in member.roles)


def _build_toss_embed(member: discord.Member, result: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🪙 {result}",
        description=f"Tossed by {member.mention}",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    return embed


class TossCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if message.channel.name != TOSS_CHANNEL_NAME:
            return

        content = (message.content or "").strip()

        try:
            await message.delete()
        except Exception:
            pass

        if content.lower() == "toss":
            if not _can_toss(message.author):
                try:
                    await message.channel.send(
                        f"⛔ {message.author.mention} — only **CAP / TABBY / ORG** may toss coins here.",
                        delete_after=6,
                    )
                except Exception:
                    pass
                return

            result = random.choice(["HEADS", "TAILS"])
            try:
                await message.channel.send(embed=_build_toss_embed(message.author, result))
            except Exception:
                pass
            return

        try:
            await message.channel.send("This channel is only for coin tosses.", delete_after=6)
        except Exception:
            pass

    @app_commands.command(name="toss", description="Flip a coin (only usable in 📨︱ga-text).")
    async def toss(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command can only be used in a server text channel.", ephemeral=True
            )
            return
        if interaction.channel.name != GA_TEXT_CHANNEL_NAME:
            await interaction.response.send_message(
                f"❌ `/toss` can only be used in **{GA_TEXT_CHANNEL_NAME}**.", ephemeral=True
            )
            return
        if not _is_non_visitor(interaction.user):
            await interaction.response.send_message("⛔ Visitors may not use this command.", ephemeral=True)
            return

        result = random.choice(["HEADS", "TAILS"])
        await interaction.response.send_message(embed=_build_toss_embed(interaction.user, result))


async def setup(bot: commands.Bot):
    await bot.add_cog(TossCog(bot))

"""
assign_cog.py — only the /mention command remains.

The old 🛅︱ASSIGN category (one channel per role, with mention-driven role assignment and mention
clean-up) was removed: roles are now given in Discord directly, through the get-role buttons, or
through the role invite links. Nothing here touches roles any more.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import ADMIN_ROLE_NAMES

log = logging.getLogger("kamla.assign")


class AssignCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="mention", description="Mention a user in a channel.")
    @app_commands.describe(user="User to mention", channel="Channel to mention them in")
    async def mention(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        channel: discord.TextChannel,
    ):
        # Organisers do not hold the Administrator permission, so Discord's own "administrators
        # only" gate would hide this command from them. The same audience (owner / admins /
        # ORG, CAP, TABBY, EQUITY) is enforced here instead.
        actor = interaction.user
        if not (actor.guild_permissions.administrator
                or any(r.name in ADMIN_ROLE_NAMES for r in actor.roles)):
            await interaction.response.send_message("⛔ Only ORG / CAP / TABBY / EQUITY can use this.", ephemeral=True)
            return
        try:
            await channel.send(user.mention)
            await interaction.response.send_message(
                f"✅ Mentioned {user.mention} in {channel.mention}.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ Cannot send messages in {channel.mention}.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AssignCog(bot))

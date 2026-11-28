import logging

import discord
from discord import app_commands
from discord.ext import commands
from config import is_organiser, bot_is_in_guild, not_a_member_message  # noqa: F401 (re-exported for callers/tests)

log = logging.getLogger("kamla.setup")

# is_organiser / bot_is_in_guild / not_a_member_message live in config.py and are re-exported here so existing
# `setup_cog.is_organiser(...)` references keep working.
# The installation itself (checks, progress, Create Now) lives in installer.py. There is no wipe and no /rebuild:
# KAMLA never deletes existing channels or roles.


async def _assign_creator_role(guild: discord.Guild, user: discord.Member, role_name: str) -> None:
    """Give whoever ran /st the organiser role they chose (directly: there is no ASSIGN category any more)."""
    from config import get_kamla_role, assign_kamla_role
    role = get_kamla_role(guild, role_name.upper())
    if role is None:
        log.warning("Creator role %s was not found in guild %s.", role_name, guild.id)
        return
    if not await assign_kamla_role(user, role):
        log.warning("Could not give %s to the creator in guild %s (role hierarchy?).", role_name, guild.id)


FORMAT_CHOICES = [
    app_commands.Choice(name="AP (Asian Parliamentary)", value="ap"),
    app_commands.Choice(name="BP (British Parliamentary)", value="bp"),
]

ROLE_CHOICES = [
    app_commands.Choice(name="ORG",    value="ORG"),
    app_commands.Choice(name="CAP",    value="CAP"),
    app_commands.Choice(name="TABBY",  value="TABBY"),
    app_commands.Choice(name="EQUITY", value="EQUITY"),
]


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="st", description="Set up the KAMLA tournament server (installer only, in server-status).")
    @app_commands.describe(
        format="Debate format",
        room="Number of debate rooms (1–1000)",
        tournamentname="Full name of the tournament",
        as_role="Your role in this tournament",
    )
    @app_commands.choices(format=FORMAT_CHOICES, as_role=ROLE_CHOICES)
    @app_commands.rename(as_role="as")
    async def st(
        self,
        interaction: discord.Interaction,
        format: app_commands.Choice[str],
        room: app_commands.Range[int, 1, 1000],
        tournamentname: str,
        as_role: app_commands.Choice[str],
    ):
        """Works ONLY in 📊︱server-status and ONLY for the person who installed KAMLA."""
        import installer
        denied = await installer.authorize_installer(interaction)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        if not bot_is_in_guild(interaction):
            await interaction.response.send_message(not_a_member_message(interaction), ephemeral=True,
                                                    suppress_embeds=True)      # contains a link: no preview
            return
        compat = installer.check_compatibility(interaction.guild)
        if not compat.ok:
            await interaction.response.send_message(embed=installer.compat_embed(compat, interaction.user), ephemeral=True)
            return
        await interaction.response.send_message("🚀 Starting the installation — follow it in this channel.", ephemeral=True)
        await installer.run_install(interaction.guild, interaction.channel, interaction.user, {
            "format": format.value, "rooms": room, "timezone": "+06:00",
            "tournament_name": tournamentname, "role_name": as_role.value,
        })


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

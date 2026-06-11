import discord
from discord.ext import commands
from config import (
    KAMLA_ROLE_NAMES, ADMIN_ROLE_NAMES,
    get_kamla_role, get_member_kamla_role, assign_kamla_role,
)

PUBLIC_ROLES = [
    "INVITED ADJUDICATOR",
    "INDEPENDENT ADJUDICATOR",
    "DEBATER",
    "VISITOR",
]

BUTTON_EMOJI = {
    "INVITED ADJUDICATOR": "🔵",
    "INDEPENDENT ADJUDICATOR": "🟣",
    "DEBATER": "🩵",
    "VISITOR": "⬜",
}


class RoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _handle_role(self, interaction: discord.Interaction, role_name: str):
        guild = interaction.guild
        member = interaction.user

        current = get_member_kamla_role(member)
        if current and current.name in ADMIN_ROLE_NAMES:
            await interaction.response.send_message(
                f"⚠️ You currently hold the **{current.name}** admin role. "
                "To change your role, please contact the tournament **ORG**.",
                ephemeral=True,
            )
            return

        role = get_kamla_role(guild, role_name)
        if role is None:
            await interaction.response.send_message(
                f"❌ The role **{role_name}** does not exist on this server yet. "
                "Please ask the server admin to run `/st` first.",
                ephemeral=True,
            )
            return

        if current and current.id == role.id:
            await interaction.response.send_message(
                f"ℹ️ You already have the **{role_name}** role.", ephemeral=True
            )
            return

        await assign_kamla_role(member, role)

        if current:
            msg = f"✅ Your role has been updated: **{current.name}** → **{role_name}**."
        else:
            msg = f"✅ You have been assigned the **{role_name}** role."

        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(
        label="Invited Adjudicator",
        style=discord.ButtonStyle.primary,
        custom_id="kamla:role:invited_adj",
        emoji="🔵",
        row=0,
    )
    async def invited_adj(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_role(interaction, "INVITED ADJUDICATOR")

    @discord.ui.button(
        label="Independent Adjudicator",
        style=discord.ButtonStyle.primary,
        custom_id="kamla:role:independent_adj",
        emoji="🟣",
        row=0,
    )
    async def independent_adj(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_role(interaction, "INDEPENDENT ADJUDICATOR")

    @discord.ui.button(
        label="Debater",
        style=discord.ButtonStyle.success,
        custom_id="kamla:role:debater",
        emoji="🩵",
        row=1,
    )
    async def debater(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_role(interaction, "DEBATER")

    @discord.ui.button(
        label="Visitor",
        style=discord.ButtonStyle.secondary,
        custom_id="kamla:role:visitor",
        emoji="⬜",
        row=1,
    )
    async def visitor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_role(interaction, "VISITOR")


class RolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(RolesCog(bot))

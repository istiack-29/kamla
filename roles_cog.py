import discord
from discord.ext import commands
from config import (
    KAMLA_ROLE_NAMES, ADMIN_ROLE_NAMES,
    get_member_kamla_role,
)

PUBLIC_ROLE_CHANNEL_MAP = {
    "INVITED ADJUDICATOR":    "invited-adjudicator",
    "INDEPENDENT ADJUDICATOR": "independent-adjudicator",
    "DEBATER":                "debater",
    "VISITOR":                "visitor",
}


async def _route_via_assign_channel(
    interaction: discord.Interaction, role_name: str
) -> None:
    """Mention user in the assign channel so AssignCog processes the role."""
    guild = interaction.guild
    member = interaction.user

    # Block admin-role holders from using get-role buttons
    existing = get_member_kamla_role(member)
    if existing and existing.name in ADMIN_ROLE_NAMES:
        await interaction.response.send_message(
            f"⚠️ You hold the **{existing.name}** admin role.\n"
            "Contact the tournament **ORG** to change your role.",
            ephemeral=True,
        )
        return

    # Find assign category
    assign_cat = discord.utils.get(guild.categories, name="🛅︱ASSIGN")
    if assign_cat is None:
        await interaction.response.send_message(
            "❌ The ASSIGN category does not exist. Ask an admin to run `/st` first.",
            ephemeral=True,
        )
        return

    # Find the specific assign channel for this role
    ch_name = PUBLIC_ROLE_CHANNEL_MAP.get(role_name)
    if ch_name is None:
        await interaction.response.send_message(
            f"❌ No assign channel mapping for **{role_name}**.", ephemeral=True
        )
        return

    assign_ch = discord.utils.get(assign_cat.text_channels, name=ch_name)
    if assign_ch is None:
        await interaction.response.send_message(
            f"❌ Channel **#{ch_name}** not found in the ASSIGN category.", ephemeral=True
        )
        return

    # Bot mentions user in assign channel → AssignCog's on_message handles assignment
    try:
        await assign_ch.send(member.mention)
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ KAMLA cannot send messages in the assign channel. Check permissions.",
            ephemeral=True,
        )
        return

    if existing:
        desc = (
            f"🔄 Switching your role from **{existing.name}** to **{role_name}**…\n"
            "Your role will update in a moment."
        )
    else:
        desc = f"🔄 Assigning you the **{role_name}** role… it will appear shortly."

    await interaction.response.send_message(desc, ephemeral=True)


class RoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Invited Adjudicator",
        style=discord.ButtonStyle.primary,
        custom_id="kamla:role:invited_adj",
        emoji="🔵",
        row=0,
    )
    async def invited_adj(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _route_via_assign_channel(interaction, "INVITED ADJUDICATOR")

    @discord.ui.button(
        label="Independent Adjudicator",
        style=discord.ButtonStyle.primary,
        custom_id="kamla:role:independent_adj",
        emoji="🟣",
        row=0,
    )
    async def independent_adj(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _route_via_assign_channel(interaction, "INDEPENDENT ADJUDICATOR")

    @discord.ui.button(
        label="Debater",
        style=discord.ButtonStyle.success,
        custom_id="kamla:role:debater",
        emoji="🩵",
        row=1,
    )
    async def debater(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _route_via_assign_channel(interaction, "DEBATER")

    @discord.ui.button(
        label="Visitor",
        style=discord.ButtonStyle.secondary,
        custom_id="kamla:role:visitor",
        emoji="⬜",
        row=1,
    )
    async def visitor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _route_via_assign_channel(interaction, "VISITOR")


class RolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(RolesCog(bot))

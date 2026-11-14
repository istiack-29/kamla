import discord
from discord.ext import commands
from config import (
    ADMIN_ROLE_NAMES,
    assign_kamla_role,
    get_member_kamla_role,
)

async def _assign_role(interaction: discord.Interaction, role_name: str) -> None:
    """Give the clicking member `role_name` (replacing any other tournament role they hold).

    This used to post a mention in a per-role channel of the 🛅︱ASSIGN category and let another listener
    assign the role. That category no longer exists, so the role is assigned right here."""
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

    role = next((r for r in guild.roles if r.name == role_name and not r.managed), None)
    if role is None:
        await interaction.response.send_message(
            f"❌ The **{role_name}** role does not exist. Ask an admin to run `/st` first.", ephemeral=True)
        return
    if existing and existing.id == role.id:
        await interaction.response.send_message(f"You already have the **{role_name}** role.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    if not await assign_kamla_role(member, role):
        await interaction.followup.send(
            "❌ KAMLA could not give you that role. An admin must move **KAMLA's role above the tournament roles** "
            "(Server Settings → Roles).", ephemeral=True)
        return
    if existing:
        await interaction.followup.send(f"🔄 Switched your role from **{existing.name}** to **{role_name}**.", ephemeral=True)
    else:
        await interaction.followup.send(f"✅ You are now **{role_name}**.", ephemeral=True)


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
        await _assign_role(interaction, "INVITED ADJUDICATOR")

    @discord.ui.button(
        label="Independent Adjudicator",
        style=discord.ButtonStyle.primary,
        custom_id="kamla:role:independent_adj",
        emoji="🟣",
        row=0,
    )
    async def independent_adj(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _assign_role(interaction, "INDEPENDENT ADJUDICATOR")

    @discord.ui.button(
        label="Debater",
        style=discord.ButtonStyle.success,
        custom_id="kamla:role:debater",
        emoji="🩵",
        row=1,
    )
    async def debater(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _assign_role(interaction, "DEBATER")

    @discord.ui.button(
        label="Visitor",
        style=discord.ButtonStyle.secondary,
        custom_id="kamla:role:visitor",
        emoji="⬜",
        row=1,
    )
    async def visitor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _assign_role(interaction, "VISITOR")


class RolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(RolesCog(bot))

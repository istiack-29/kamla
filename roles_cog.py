"""roles_cog.py — the Get Role panel.

A single select menu (works well on phones: big tap target, one description per role) replaces the old four buttons.
The panel never creates roles — it hands out the tournament roles that /st created — and it never touches KAMLA's
own (bot) role. The four legacy button ids stay registered so panels posted by an older KAMLA keep working.
"""

import logging

import discord
from discord.ext import commands

from config import ADMIN_ROLE_NAMES, assign_kamla_role, get_member_kamla_role, kamla_role

log = logging.getLogger("kamla.roles")

# (role name, emoji, label, description shown in the menu — max 100 characters)
ROLE_INFO = [
    ("INVITED ADJUDICATOR", "🔵", "Invited Adjudicator", "Officially invited judge of the tournament"),
    ("INDEPENDENT ADJUDICATOR", "🟣", "Independent Adjudicator", "Self-registered judge"),
    ("DEBATER", "🩵", "Debater", "A competing speaker"),
    ("VISITOR", "⬜", "Visitor", "Observer or guest (read-only access to rooms)"),
]
SELECT_ID = "kamla:role:select"

_busy: set[tuple[int, int]] = set()          # (guild, member) with a role change in progress → no double assignment


def build_get_role_embed(guild: discord.Guild | None = None) -> discord.Embed:
    owner = f"<@{guild.owner_id}>" if guild is not None and getattr(guild, "owner_id", None) else "the tournament ORG"
    embed = discord.Embed(
        title="🎭 Get Your Role",
        description=("Open the menu below and pick the role that matches you.\n"
                     "You hold **one role** at a time — choosing another one replaces the previous one."),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Roles",
        value="\n".join(f"{emoji} **{label}** — {desc}" for _n, emoji, label, desc in ROLE_INFO),
        inline=False,
    )
    embed.add_field(
        name="🎖️ CA · Equity · Tabby · Org",
        value=f"These are given by organisers only — message the server owner {owner}.",
        inline=False,
    )
    embed.set_footer(text="KAMLA • Role changes are instant")
    return embed


async def _respond(interaction: discord.Interaction, text: str, *, followup: bool) -> None:
    """Answer an interaction without ever raising: an expired interaction (older than 3 s / 15 min) is simply dropped."""
    try:
        if followup or interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.NotFound:
        log.info("Role interaction expired before KAMLA could answer it.")
    except discord.HTTPException as e:
        log.warning("Could not answer a role interaction: %s", e)


async def _assign_role(interaction: discord.Interaction, role_name: str) -> None:
    """Give the member `role_name` (replacing any other tournament role they hold)."""
    guild, member = interaction.guild, interaction.user
    if guild is None or member is None:
        await _respond(interaction, "This only works inside the server.", followup=False)
        return

    existing = get_member_kamla_role(member)
    if existing and existing.name in ADMIN_ROLE_NAMES:
        await _respond(interaction, f"⚠️ You hold the **{existing.name}** admin role.\n"
                                    "Contact the tournament **ORG** to change your role.", followup=False)
        return

    role = next((r for r in guild.roles if r.name == role_name and not r.managed), None)
    if role is None:
        await _respond(interaction, f"❌ The **{role_name}** role does not exist yet. An admin must finish `/st` first.",
                       followup=False)
        return
    if existing and existing.id == role.id:
        await _respond(interaction, f"You already have the **{role_name}** role — nothing to change.", followup=False)
        return

    me = getattr(guild, "me", None)
    if me is not None:
        perms = getattr(me, "guild_permissions", None)
        if perms is not None and not (getattr(perms, "manage_roles", False) or getattr(perms, "administrator", False)):
            await _respond(interaction, "❌ KAMLA is missing the **Manage Roles** permission. Please tell an admin.",
                           followup=False)
            return
        top = getattr(me, "top_role", None) or kamla_role(guild)
        if top is not None and getattr(role, "position", -1) >= getattr(top, "position", 10**9):
            await _respond(interaction, "❌ KAMLA's role is **below** the tournament roles, so it cannot give this one. "
                                        "An admin must drag **KAMLA's role to the top** (Server Settings → Roles).",
                           followup=False)
            return

    key = (getattr(guild, "id", 0), member.id)
    if key in _busy:
        await _respond(interaction, "⏳ Your role change is already being processed.", followup=False)
        return
    _busy.add(key)
    try:
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            log.info("Role interaction expired before it could be deferred.")
            return
        if not await assign_kamla_role(member, role):
            await _respond(interaction, "❌ KAMLA could not give you that role. An admin must move **KAMLA's role above "
                                        "the tournament roles** (Server Settings → Roles).", followup=True)
            return
        if existing:
            await _respond(interaction, f"🔄 Switched your role from **{existing.name}** to **{role_name}**.", followup=True)
        else:
            await _respond(interaction, f"✅ You are now **{role_name}**.", followup=True)
    finally:
        _busy.discard(key)


class RoleSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            custom_id=SELECT_ID,
            placeholder="Choose your role…",
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=label, value=name, emoji=emoji, description=desc)
                     for name, emoji, label, desc in ROLE_INFO],
        )

    async def callback(self, interaction: discord.Interaction):
        await _assign_role(interaction, self.values[0])


class RoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RoleSelect())


class LegacyRoleView(discord.ui.View):
    """The four buttons of the old Get Role panel (same custom ids), so messages posted before the redesign work."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Invited Adjudicator", style=discord.ButtonStyle.primary,
                       custom_id="kamla:role:invited_adj", emoji="🔵", row=0)
    async def invited_adj(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _assign_role(interaction, "INVITED ADJUDICATOR")

    @discord.ui.button(label="Independent Adjudicator", style=discord.ButtonStyle.primary,
                       custom_id="kamla:role:independent_adj", emoji="🟣", row=0)
    async def independent_adj(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _assign_role(interaction, "INDEPENDENT ADJUDICATOR")

    @discord.ui.button(label="Debater", style=discord.ButtonStyle.success,
                       custom_id="kamla:role:debater", emoji="🩵", row=1)
    async def debater(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _assign_role(interaction, "DEBATER")

    @discord.ui.button(label="Visitor", style=discord.ButtonStyle.secondary,
                       custom_id="kamla:role:visitor", emoji="⬜", row=1)
    async def visitor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _assign_role(interaction, "VISITOR")


class RolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(RolesCog(bot))

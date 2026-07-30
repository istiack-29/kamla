"""
dm_notify_cog.py — KAMLA DM notification system.

Sends a DM to the user when:
  • They join the server
  • They receive or change a role
  • They are kicked or banned
  • They leave the server
"""

import discord
from discord.ext import commands
from config import KAMLA_ROLE_NAMES


async def _dm(user: discord.User | discord.Member, embed: discord.Embed) -> None:
    """Send a DM; silently ignore if DMs are closed."""
    try:
        await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


class DMNotifyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Join ─────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        embed = discord.Embed(
            title="👋 Welcome!",
            description=(
                f"You have joined **{member.guild.name}**.\n\n"
                "Head to **❓︱get-role** to claim your role and get access to the server."
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"KAMLA • {member.guild.name}")
        if member.guild.icon:
            embed.set_thumbnail(url=member.guild.icon.url)
        await _dm(member, embed)

    # ── Role change ───────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        before_roles = {r.name for r in before.roles}
        after_roles  = {r.name for r in after.roles}

        added   = after_roles  - before_roles
        removed = before_roles - after_roles

        # Only notify for KAMLA roles (avoid spamming for unrelated role changes)
        added_kamla   = [n for n in added   if n in KAMLA_ROLE_NAMES]
        removed_kamla = [n for n in removed if n in KAMLA_ROLE_NAMES]

        if not added_kamla and not removed_kamla:
            return

        guild = after.guild

        if added_kamla and removed_kamla:
            # Role switch
            embed = discord.Embed(
                title="🔄 Role Changed",
                description=(
                    f"Your role in **{guild.name}** has been updated.\n\n"
                    f"**Removed:** {', '.join(f'`{r}`' for r in removed_kamla)}\n"
                    f"**Assigned:** {', '.join(f'`{r}`' for r in added_kamla)}"
                ),
                color=discord.Color.orange(),
            )
        elif added_kamla:
            embed = discord.Embed(
                title="✅ Role Assigned",
                description=(
                    f"You have been assigned a role in **{guild.name}**.\n\n"
                    f"**Role:** {', '.join(f'`{r}`' for r in added_kamla)}"
                ),
                color=discord.Color.blurple(),
            )
        else:
            embed = discord.Embed(
                title="❌ Role Removed",
                description=(
                    f"A role has been removed from you in **{guild.name}**.\n\n"
                    f"**Role:** {', '.join(f'`{r}`' for r in removed_kamla)}"
                ),
                color=discord.Color.red(),
            )

        embed.set_footer(text=f"KAMLA • {guild.name}")
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        await _dm(after, embed)

    # ── Leave ─────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild

        # Check audit log to distinguish kick / ban from voluntary leave
        action = "left"
        try:
            async for entry in guild.audit_logs(limit=5):
                if entry.target and entry.target.id == member.id:
                    if entry.action == discord.AuditLogAction.kick:
                        action = "kicked"
                    elif entry.action == discord.AuditLogAction.ban:
                        action = "banned"
                    break
        except discord.Forbidden:
            pass

        if action == "kicked":
            embed = discord.Embed(
                title="🦵 You Were Kicked",
                description=(
                    f"You have been **kicked** from **{guild.name}**.\n\n"
                    "If you believe this was a mistake, contact the server organisers."
                ),
                color=discord.Color.red(),
            )
        elif action == "banned":
            embed = discord.Embed(
                title="🔨 You Were Banned",
                description=(
                    f"You have been **banned** from **{guild.name}**.\n\n"
                    "If you believe this was a mistake, contact the server organisers."
                ),
                color=discord.Color.dark_red(),
            )
        else:
            embed = discord.Embed(
                title="👋 You Left the Server",
                description=f"You have left **{guild.name}**. Hope to see you again!",
                color=discord.Color.greyple(),
            )

        embed.set_footer(text=f"KAMLA • {guild.name}")
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        await _dm(member, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(DMNotifyCog(bot))

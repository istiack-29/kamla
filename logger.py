"""
KAMLABot — logger.py
Cog: AuditLogger

Global Audit Logging — binds to Discord event listeners and logs all
structural/permission changes to #server-audit-logs in clean embeds.

Events tracked:
  on_guild_channel_create   — New channel/category created
  on_guild_channel_delete   — Channel/category deleted
  on_guild_channel_update   — Channel renamed, permissions changed, etc.
  on_guild_role_create      — New role created
  on_guild_role_delete      — Role deleted
  on_guild_role_update      — Role renamed, permission edited, etc.

Channel visibility:
  #server-audit-logs is visible ONLY to ORG, CAP, Tabby, and Equity Officer.
  (Created by base_builder.py; ID stored in bot.state["channels"]["audit_logs"])
"""

import discord
from discord.ext import commands
from datetime import timezone


class AuditLogger(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def log(self, guild: discord.Guild, embed: discord.Embed):
        """
        Post an embed to #server-audit-logs.
        Public helper — called by other Cogs (commands.py etc.) to log events.
        """
        audit_ch_id = self.bot.state["channels"].get("audit_logs")
        if not audit_ch_id:
            return

        channel = guild.get_channel(audit_ch_id)
        if not channel:
            return

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[AuditLogger] Failed to send log: {e}")

    # ── Channel events ────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        embed = discord.Embed(
            title="📢 Channel Created",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Name", value=channel.name, inline=True)
        embed.add_field(name="Type", value=str(channel.type).replace("_", " ").title(), inline=True)
        if hasattr(channel, "category") and channel.category:
            embed.add_field(name="Category", value=channel.category.name, inline=True)
        embed.set_footer(text=f"Channel ID: {channel.id}")
        await self.log(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        embed = discord.Embed(
            title="🗑️ Channel Deleted",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Name", value=channel.name, inline=True)
        embed.add_field(name="Type", value=str(channel.type).replace("_", " ").title(), inline=True)
        if hasattr(channel, "category") and channel.category:
            embed.add_field(name="Category", value=channel.category.name, inline=True)
        embed.set_footer(text=f"Channel ID: {channel.id}")
        await self.log(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ):
        changes = []

        # Name change
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")

        # Category change
        before_cat = getattr(before, "category", None)
        after_cat = getattr(after, "category", None)
        if before_cat != after_cat:
            before_name = before_cat.name if before_cat else "None"
            after_name = after_cat.name if after_cat else "None"
            changes.append(f"**Category:** `{before_name}` → `{after_name}`")

        # Slowmode change
        before_slow = getattr(before, "slowmode_delay", None)
        after_slow = getattr(after, "slowmode_delay", None)
        if before_slow != after_slow:
            changes.append(f"**Slowmode:** `{before_slow}s` → `{after_slow}s`")

        # NSFW change
        before_nsfw = getattr(before, "nsfw", None)
        after_nsfw = getattr(after, "nsfw", None)
        if before_nsfw != after_nsfw:
            changes.append(f"**NSFW:** `{before_nsfw}` → `{after_nsfw}`")

        # Permission overwrite changes
        before_ow = getattr(before, "overwrites", {})
        after_ow = getattr(after, "overwrites", {})
        if before_ow != after_ow:
            changes.append("**Permissions:** Updated (overwrite changes detected)")

        if not changes:
            return

        embed = discord.Embed(
            title="✏️ Channel Updated",
            description="\n".join(changes),
            color=discord.Color.yellow(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Channel", value=f"<#{after.id}> (`{after.name}`)", inline=True)
        embed.set_footer(text=f"Channel ID: {after.id}")
        await self.log(after.guild, embed)

    # ── Role events ───────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        embed = discord.Embed(
            title="🎭 Role Created",
            color=role.color if role.color != discord.Color.default() else discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Name", value=role.name, inline=True)
        embed.add_field(name="Mentionable", value=str(role.mentionable), inline=True)
        embed.add_field(name="Hoisted", value=str(role.hoist), inline=True)
        embed.set_footer(text=f"Role ID: {role.id}")
        await self.log(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        embed = discord.Embed(
            title="❌ Role Deleted",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Name", value=role.name, inline=True)
        embed.set_footer(text=f"Role ID: {role.id}")
        await self.log(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []

        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")
        if before.color != after.color:
            changes.append(f"**Color:** `{before.color}` → `{after.color}`")
        if before.hoist != after.hoist:
            changes.append(f"**Hoisted:** `{before.hoist}` → `{after.hoist}`")
        if before.mentionable != after.mentionable:
            changes.append(f"**Mentionable:** `{before.mentionable}` → `{after.mentionable}`")
        if before.permissions != after.permissions:
            changes.append("**Permissions:** Updated")

        if not changes:
            return

        embed = discord.Embed(
            title="🔄 Role Updated",
            description="\n".join(changes),
            color=after.color if after.color != discord.Color.default() else discord.Color.yellow(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Role", value=f"{after.mention} (`{after.name}`)", inline=True)
        embed.set_footer(text=f"Role ID: {after.id}")
        await self.log(after.guild, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(AuditLogger(bot))

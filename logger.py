"""
KAMLABot — logger.py
Cog: AuditLogger

Global audit logger. Posts structural/permission changes to #server-audit-logs.

Setup Silence Protocol:
    All listeners short-circuit when bot.state["is_setup_active"] is True,
    so the channel isn't spammed during /startb nuke + rebuild.
"""

import discord
from discord.ext import commands


HC_KEYS = ("ORG", "CAP", "Tabby", "Equity Officer")


class AuditLogger(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Internal helpers ─────────────────────────────────────────────────────
    def _silenced(self) -> bool:
        return bool(self.bot.state.get("is_setup_active"))

    async def log(self, guild: discord.Guild, embed: discord.Embed):
        """Public helper: post an embed to #server-audit-logs."""
        if self._silenced():
            return
        ch_id = self.bot.state["channels"].get("audit_logs")
        if not ch_id:
            return
        channel = guild.get_channel(ch_id)
        if not channel:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[AuditLogger] {e}")

    def _base_embed(self, title: str, color: discord.Color) -> discord.Embed:
        return discord.Embed(
            title=title,
            color=color,
            timestamp=discord.utils.utcnow(),
        )

    # ── Channel events ───────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if self._silenced():
            return
        e = self._base_embed("📢 Channel Created", discord.Color.green())
        e.add_field(name="Name", value=channel.name, inline=True)
        e.add_field(name="Type", value=str(channel.type).replace("_", " ").title(), inline=True)
        if getattr(channel, "category", None):
            e.add_field(name="Category", value=channel.category.name, inline=True)
        e.set_footer(text=f"Channel ID: {channel.id}")
        await self.log(channel.guild, e)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if self._silenced():
            return
        e = self._base_embed("🗑️ Channel Deleted", discord.Color.red())
        e.add_field(name="Name", value=channel.name, inline=True)
        e.add_field(name="Type", value=str(channel.type).replace("_", " ").title(), inline=True)
        e.set_footer(text=f"Channel ID: {channel.id}")
        await self.log(channel.guild, e)

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ):
        if self._silenced():
            return
        changes = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`"))
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append(("Topic", f"`{getattr(before, 'topic', '')}` → `{getattr(after, 'topic', '')}`"))
        if before.overwrites != after.overwrites:
            changes.append(("Permissions", "Overwrites updated"))
        if not changes:
            return
        e = self._base_embed("✏️ Channel Updated", discord.Color.orange())
        e.add_field(name="Channel", value=after.mention if hasattr(after, "mention") else after.name, inline=False)
        for k, v in changes:
            e.add_field(name=k, value=v[:1024], inline=False)
        e.set_footer(text=f"Channel ID: {after.id}")
        await self.log(after.guild, e)

    # ── Role events ──────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        if self._silenced():
            return
        e = self._base_embed("✨ Role Created", discord.Color.green())
        e.add_field(name="Name", value=role.name, inline=True)
        e.add_field(name="Color", value=str(role.color), inline=True)
        e.set_footer(text=f"Role ID: {role.id}")
        await self.log(role.guild, e)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if self._silenced():
            return
        e = self._base_embed("🗑️ Role Deleted", discord.Color.red())
        e.add_field(name="Name", value=role.name, inline=True)
        e.set_footer(text=f"Role ID: {role.id}")
        await self.log(role.guild, e)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if self._silenced():
            return
        changes = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`"))
        if before.color != after.color:
            changes.append(("Color", f"{before.color} → {after.color}"))
        if before.permissions != after.permissions:
            changes.append(("Permissions", "Permission bitset updated"))
        if not changes:
            return
        e = self._base_embed("✏️ Role Updated", discord.Color.orange())
        e.add_field(name="Role", value=after.mention, inline=False)
        for k, v in changes:
            e.add_field(name=k, value=v, inline=False)
        e.set_footer(text=f"Role ID: {after.id}")
        await self.log(after.guild, e)


async def setup(bot: commands.Bot):
    await bot.add_cog(AuditLogger(bot))

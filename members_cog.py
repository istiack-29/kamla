"""members_cog.py — the 👥︱members-update log: joins, leaves, bans and unbans, with the responsible moderator
when Discord's audit log provides one.

Accuracy rules:
  * Discord fires `member_remove` for a leave, a kick AND a ban, plus `member_ban` for a ban. The audit log decides:
    a ban is logged once (by `member_ban`), a kick is logged as a kick, everything else as a leave.
  * Every (kind, user) entry is de-duplicated for a few seconds, so a gateway replay cannot double-post.
  * The moderator is only named when an audit entry for exactly that user and action was created just now;
    otherwise the log says so ("not available") instead of guessing.
This channel is read-only for people (guards.py); only KAMLA writes here. Nothing here ever sends a DM.
"""

import asyncio
import logging

import discord
from discord.ext import commands

from naming import CH_MEMBERS
from ratelimit import notify, describe

log = logging.getLogger("kamla.members")

DEDUP_SECONDS = 15.0
AUDIT_WAIT = 1.5               # audit entries appear slightly after the gateway event
AUDIT_MAX_AGE = 20.0           # an entry older than this belongs to an earlier action
_recent: dict[tuple[int, str, int], float] = {}


def _duplicate(guild_id: int, kind: str, user_id: int) -> bool:
    now = asyncio.get_running_loop().time()
    for k in [k for k, t in _recent.items() if now - t > DEDUP_SECONDS]:
        _recent.pop(k, None)
    key = (guild_id, kind, user_id)
    if key in _recent:
        return True
    _recent[key] = now
    return False


def _channel(guild: discord.Guild):
    return discord.utils.get(guild.text_channels, name=CH_MEMBERS)


async def find_audit_entry(guild: discord.Guild, action: discord.AuditLogAction, user_id: int):
    """The newest audit entry for `action` against `user_id` created within AUDIT_MAX_AGE seconds, else None."""
    me = guild.me
    if me is None or not (me.guild_permissions.view_audit_log or me.guild_permissions.administrator):
        return None
    for attempt in range(2):
        await asyncio.sleep(AUDIT_WAIT if attempt == 0 else AUDIT_WAIT * 2)
        try:
            async for entry in guild.audit_logs(limit=10, action=action):
                target = getattr(entry, "target", None)
                if getattr(target, "id", None) != user_id:
                    continue
                age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                if age <= AUDIT_MAX_AGE:
                    return entry
        except (discord.Forbidden, discord.HTTPException) as e:
            log.debug("audit log unavailable: %s", describe(e))
            return None
    return None


def _entry_embed(title: str, color: discord.Color, user, *, moderator=None, reason: str | None = None,
                 audit_checked: bool = False, extra: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
    embed.description = f"{user.mention} • **{user}** • `{user.id}`"
    if extra:
        embed.description += f"\n{extra}"
    if audit_checked:
        embed.add_field(name="Moderator", value=moderator.mention if moderator else "Not available", inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason[:500], inline=True)
    try:
        embed.set_thumbnail(url=user.display_avatar.url)
    except Exception:  # noqa: BLE001
        pass
    return embed


class MembersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _post(self, guild: discord.Guild, embed: discord.Embed, label: str) -> None:
        channel = _channel(guild)
        if channel is None:
            return
        await notify.best_effort(lambda: channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none()),
                                 label=label)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if _duplicate(member.guild.id, "join", member.id):
            return
        age = ""
        try:
            created = (discord.utils.utcnow() - member.created_at).days
            age = f"Account age: {created} day(s)"
        except Exception:  # noqa: BLE001
            pass
        title = "🤖 Bot added" if member.bot else "📥 Member joined"
        await self._post(member.guild, _entry_embed(title, discord.Color.green(), member, extra=age or None), "member-join")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        if _duplicate(guild.id, "remove", member.id):
            return
        # A ban is logged by on_member_ban; only a kick or a plain leave is logged here.
        ban = await find_audit_entry(guild, discord.AuditLogAction.ban, member.id)
        if ban is not None:
            return
        kick = await find_audit_entry(guild, discord.AuditLogAction.kick, member.id)
        if kick is not None:
            embed = _entry_embed("👢 Member kicked", discord.Color.orange(), member, moderator=kick.user,
                                 reason=kick.reason, audit_checked=True)
        else:
            embed = _entry_embed("📤 Member left", discord.Color.light_grey(), member)
        await self._post(guild, embed, "member-leave")

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.abc.User):
        if _duplicate(guild.id, "ban", user.id):
            return
        entry = await find_audit_entry(guild, discord.AuditLogAction.ban, user.id)
        embed = _entry_embed("🔨 Member banned", discord.Color.red(), user, moderator=getattr(entry, "user", None),
                             reason=getattr(entry, "reason", None), audit_checked=True)
        await self._post(guild, embed, "member-ban")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        if _duplicate(guild.id, "unban", user.id):
            return
        entry = await find_audit_entry(guild, discord.AuditLogAction.unban, user.id)
        embed = _entry_embed("♻️ Member unbanned", discord.Color.blue(), user, moderator=getattr(entry, "user", None),
                             reason=getattr(entry, "reason", None), audit_checked=True)
        await self._post(guild, embed, "member-unban")


async def setup(bot: commands.Bot):
    await bot.add_cog(MembersCog(bot))

"""security_cog.py — malicious-bot prevention for the 🚨︱security-alert channel.

What it does (and the panel says exactly this):
  * The channel is visible to everyone and writable by nobody. A message that still appears there can only come from
    an account that bypasses channel permissions (an Administrator, or a bot with elevated rights).
  * Every such message is deleted.
  * A HUMAN is never moderated: their message is deleted and they get a short explanation. Nothing else happens.
  * A BOT that is not KAMLA and not on the allow-list is removed from the server (kicked) after a second message
    within 10 minutes. The first one only gets a warning.
  * Safeguards against accidental mass moderation: the owner, KAMLA, allow-listed bots and anyone whose role is not
    below KAMLA's are never kicked; at most 3 kicks per 10 minutes server-wide (then it only alerts the organisers).
  * A newly added bot is announced here (who added it) — announced only, never removed for joining.
The counters in the panel embed are live and persisted in the config channel (cfg["security"]).
"""

import asyncio
import logging
import time

import discord
from discord.ext import commands

from config import config_manager
from guards import is_kamla_message
from naming import CH_SECURITY
from ratelimit import notify, describe

log = logging.getLogger("kamla.security")

STRIKE_WINDOW = 600.0        # seconds in which a bot's messages count together
KICK_AFTER = 2               # messages inside the window before a bot is removed
MAX_KICKS = 3                # server-wide kicks inside STRIKE_WINDOW (circuit breaker)
PANEL_DEBOUNCE = 5.0

_strikes: dict[tuple[int, int], list[float]] = {}
_kick_times: dict[int, list[float]] = {}
_panel_tasks: dict[int, asyncio.Task] = {}
_clock = time.monotonic


def counters(cfg: dict) -> dict:
    sec = cfg.get("security") or {}
    return {"kicks": int(sec.get("kicks", 0)), "deleted": int(sec.get("deleted", 0)), "alerts": int(sec.get("alerts", 0))}


def build_security_embed(guild: discord.Guild, cfg: dict) -> discord.Embed:
    c = counters(cfg)
    org = discord.utils.get(getattr(guild, "roles", []), name="ORG")
    owner = f"<@{guild.owner_id}>" if getattr(guild, "owner_id", None) else None
    contacts = " ".join(x for x in (org.mention if org else None, owner) if x) or "the server organisers"
    embed = discord.Embed(
        title="🚨 SECURITY NOTICE — Malicious-bot protection",
        description=(
            "# ⚠️ DO NOT WRITE IN THIS CHANNEL\n"
            "It is **read-only** and watched by KAMLA. Anything posted here is **deleted immediately**."
        ),
        color=discord.Color.red(),
    )
    embed.add_field(
        name="What can happen",
        value=("• A **person** who posts here only has the message deleted — no penalty.\n"
               "• A **bot** that is not on the allow-list and posts here twice within 10 minutes is "
               "**removed from the server**.\n"
               "• KAMLA never removes the owner, itself, allow-listed bots, or anyone ranked above it, and stops "
               f"after {MAX_KICKS} removals per 10 minutes to avoid accidents."),
        inline=False)
    embed.add_field(name="Report / questions", value=f"{contacts} — see the other channels for support.", inline=False)
    embed.add_field(name="🦵 Bots removed", value=f"**{c['kicks']}**", inline=True)
    embed.add_field(name="🗑️ Messages deleted", value=f"**{c['deleted']}**", inline=True)
    embed.add_field(name="🔔 Alerts", value=f"**{c['alerts']}**", inline=True)
    embed.set_footer(text="KAMLA • live counters, saved across restarts")
    return embed


async def post_security_panel(channel: discord.TextChannel) -> discord.Message:
    cfg = config_manager.get_cached(channel.guild.id) or {}
    msg = await channel.send(embed=build_security_embed(channel.guild, cfg))
    sec = dict(cfg.get("security") or {})
    sec["message_id"] = msg.id
    await config_manager.update_config(channel.guild, security=sec)
    return msg


async def _bump(guild: discord.Guild, **inc: int) -> dict:
    cfg = await config_manager.get_config(guild)
    sec = dict(cfg.get("security") or {})
    for k, v in inc.items():
        sec[k] = int(sec.get(k, 0)) + v
    cfg = await config_manager.update_config(guild, security=sec)
    schedule_panel_refresh(guild)
    return cfg


def schedule_panel_refresh(guild: discord.Guild) -> None:
    """Debounced edit of the panel embed: a burst of events becomes one edit."""
    task = _panel_tasks.get(guild.id)
    if task is not None and not task.done():
        return

    async def later():
        try:
            await asyncio.sleep(PANEL_DEBOUNCE)
            await refresh_panel(guild)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Could not refresh the security panel of guild %s: %s", guild.id, describe(e))
        finally:
            _panel_tasks.pop(guild.id, None)
    _panel_tasks[guild.id] = asyncio.get_running_loop().create_task(later())


async def refresh_panel(guild: discord.Guild) -> bool:
    channel = discord.utils.get(guild.text_channels, name=CH_SECURITY)
    cfg = config_manager.get_cached(guild.id) or {}
    mid = (cfg.get("security") or {}).get("message_id")
    if channel is None or not mid:
        return False
    try:
        msg = await channel.fetch_message(int(mid))
        await msg.edit(embed=build_security_embed(guild, cfg))
        return True
    except discord.NotFound:
        return False


def _can_kick(guild: discord.Guild, member) -> str | None:
    """None when KAMLA may remove this member, otherwise the reason it must not."""
    if not hasattr(member, "top_role"):                 # a plain User (already left) cannot be kicked
        return "not a server member"
    if member.id == getattr(guild, "owner_id", None):
        return "server owner"
    if guild.me is not None and member.id == guild.me.id:
        return "KAMLA itself"
    me_top = getattr(guild.me, "top_role", None)
    their_top = getattr(member, "top_role", None)
    if me_top is not None and their_top is not None and their_top.position >= me_top.position:
        return "their role is not below KAMLA's"
    if not getattr(getattr(guild.me, "guild_permissions", None), "kick_members", False) and not getattr(
            getattr(guild.me, "guild_permissions", None), "administrator", False):
        return "KAMLA lacks the Kick Members permission"
    return None


class SecurityCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_unload(self) -> None:
        for t in list(_panel_tasks.values()):
            t.cancel()
        _panel_tasks.clear()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or getattr(message.channel, "name", None) != CH_SECURITY:
            return
        if is_kamla_message(message, self.bot):
            return
        guild, author, channel = message.guild, message.author, message.channel
        await notify.best_effort(message.delete, label="security-delete")
        await _bump(guild, deleted=1)

        if not author.bot:
            # People are never moderated here: explain, nothing more.
            await notify.best_effort(
                lambda: channel.send(f"{author.mention} this channel is read-only — your message was removed. "
                                     "No further action was taken.", delete_after=10,
                                     allowed_mentions=discord.AllowedMentions(users=[author])),
                label="security-note")
            return
        await self._handle_bot(guild, channel, author)

    async def _handle_bot(self, guild, channel, bot_member) -> None:
        cfg = await config_manager.get_config(guild)
        allow = {int(x) for x in cfg.get("security_allowlist", []) if str(x).isdigit()}
        if bot_member.id in allow:
            return
        now = _clock()
        key = (guild.id, bot_member.id)
        hits = [t for t in _strikes.get(key, []) if now - t < STRIKE_WINDOW] + [now]
        _strikes[key] = hits
        if len(hits) < KICK_AFTER:
            await notify.best_effort(
                lambda: channel.send(f"⚠️ {bot_member.mention} is not allowed to post here. One more message within "
                                     "10 minutes and it will be removed from the server.", delete_after=30,
                                     allowed_mentions=discord.AllowedMentions.none()), label="security-warn")
            return
        recent = [t for t in _kick_times.get(guild.id, []) if now - t < STRIKE_WINDOW]
        _kick_times[guild.id] = recent
        reason = _can_kick(guild, bot_member)
        if reason is None and len(recent) >= MAX_KICKS:
            reason = f"kick limit reached ({MAX_KICKS} per 10 minutes)"
        if reason is not None:
            await _bump(guild, alerts=1)
            await notify.best_effort(
                lambda: channel.send(f"🚨 {bot_member.mention} keeps posting here but was **not** removed ({reason}). "
                                     "Organisers: please review it.", allowed_mentions=discord.AllowedMentions.none()),
                label="security-alert")
            return
        ok = await notify.best_effort(
            lambda: bot_member.kick(reason="KAMLA security: bot posted repeatedly in the security channel"),
            label="security-kick")
        if ok:
            _kick_times.setdefault(guild.id, []).append(now)
            _strikes.pop(key, None)
            await _bump(guild, kicks=1)
            await notify.best_effort(
                lambda: channel.send(f"🦵 Removed bot **{bot_member}** (`{bot_member.id}`) after repeated unauthorized "
                                     "messages here.", allowed_mentions=discord.AllowedMentions.none()),
                label="security-log")
        else:
            await _bump(guild, alerts=1)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """A new bot is announced (who added it), never removed just for joining."""
        if not member.bot or member.id == getattr(self.bot.user, "id", None):
            return
        channel = discord.utils.get(member.guild.text_channels, name=CH_SECURITY)
        if channel is None:
            return
        adder = None
        try:
            await asyncio.sleep(1)
            async for entry in member.guild.audit_logs(action=discord.AuditLogAction.bot_add, limit=5):
                if entry.target and entry.target.id == member.id:
                    adder = entry.user
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass
        who = f"by {adder.mention}" if adder else "(who added it is unavailable)"
        await _bump(member.guild, alerts=1)
        await notify.best_effort(
            lambda: channel.send(f"🤖 New bot joined: **{member}** (`{member.id}`) added {who}. "
                                 "If this was not expected, remove it.", allowed_mentions=discord.AllowedMentions.none()),
            label="security-bot-join")


async def setup(bot: commands.Bot):
    await bot.add_cog(SecurityCog(bot))

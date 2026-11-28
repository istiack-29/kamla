"""guards.py — the bot-side half of two strict rules (the permission overwrites are the other half).

1. READ-ONLY CHANNELS (👥︱members-update, ⚙️︱kamla-config, 🛠️︱settings, 👨‍💻︱meet-our-developers)
   Overwrites already deny Send Messages, but a server owner and every Administrator bypass overwrites. So any message
   in these channels that is not KAMLA's own is deleted immediately — the owner's included.

2. NO THREADS
   Overwrites deny public/private thread creation, but Administrators bypass them. Any thread that KAMLA did not create
   is deleted as soon as it appears (this also catches threads started from a message), and every new channel gets the
   thread lock right away.

Both only act in servers where KAMLA is installed (the config exists), never in a server it merely sits in.
"""

import asyncio
import logging

import discord
from discord.ext import commands

from config import config_manager, thread_locked, THREAD_DENY
from naming import READ_ONLY_CHANNELS
from ratelimit import bulk, notify, describe

log = logging.getLogger("kamla.guards")


def is_kamla_message(message: discord.Message, bot: commands.Bot) -> bool:
    """KAMLA's own legitimate messages: sent by the bot user, or by a webhook that belongs to the bot's application."""
    me = getattr(bot, "user", None)
    if me is not None and getattr(message.author, "id", None) == me.id:
        return True
    app_id = getattr(message, "application_id", None)
    if getattr(message, "webhook_id", None) and app_id and app_id == getattr(bot, "application_id", None):
        return True
    return False


def is_read_only_channel(channel) -> bool:
    return isinstance(channel, discord.TextChannel) and channel.name in READ_ONLY_CHANNELS


async def guild_is_installed(guild: discord.Guild) -> bool:
    cfg = config_manager.get_cached(guild.id)
    if not cfg:
        try:
            cfg = await asyncio.wait_for(config_manager.get_config(guild), timeout=3.0)
        except Exception:  # noqa: BLE001
            return False
    return bool(cfg and (cfg.get("format") or cfg.get("installer_id")))


class GuardsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._swept = False

    # ── read-only channels ───────────────────────────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or not is_read_only_channel(message.channel):
            return
        if is_kamla_message(message, self.bot):
            return
        # Everybody else — owner, administrators, moderators, members, other bots.
        ok = await notify.best_effort(message.delete, label="read-only-delete")
        if not ok:
            log.warning("Could not delete a message in read-only channel #%s of guild %s (is Manage Messages "
                        "granted to KAMLA there?).", message.channel.name, message.guild.id)

    # ── threads ──────────────────────────────────────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        if thread.owner_id is not None and thread.owner_id == getattr(self.bot.user, "id", None):
            return
        if not await guild_is_installed(thread.guild):
            return
        if not await notify.best_effort(lambda: thread.delete(reason="KAMLA: threads are not allowed"),
                                        label="thread-delete"):
            log.warning("Could not delete thread %s in guild %s (Manage Threads missing?).", thread.id, thread.guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        """Every channel created later (by anyone) gets the thread lock too."""
        if not hasattr(channel, "overwrites_for") or thread_locked(channel):
            return
        if not await guild_is_installed(channel.guild):
            return
        await lock_channel(channel)

    # ── startup: bring existing installations up to date, one guild at a time ───────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        if self._swept:
            return
        self._swept = True
        asyncio.get_running_loop().create_task(self._sweep())

    async def _sweep(self) -> None:
        from server_builder import enforce_thread_lock, migrate_legacy_names, _building_guilds
        for guild in list(self.bot.guilds):
            try:
                if guild.id in _building_guilds or not await guild_is_installed(guild):
                    continue
                renamed = await migrate_legacy_names(guild)
                if renamed:
                    log.info("Migrated %d legacy channel name(s) in guild %s.", len(renamed), guild.id)
                import devaccess
                await devaccess.ensure(guild)
                _checked, fixed, unverified = await enforce_thread_lock(guild)
                if fixed or unverified:
                    log.info("Thread lock in guild %s: fixed %d, unverified %d.", guild.id, fixed, unverified)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Start-up sweep failed in guild %s: %s", guild.id, describe(e))
            await asyncio.sleep(1)


async def lock_channel(channel) -> bool:
    """Deny the three thread permissions to @everyone on one channel (no-op when already locked)."""
    if thread_locked(channel):
        return True
    ow = channel.overwrites_for(channel.guild.default_role)
    ow.update(**THREAD_DENY)
    return bool(await bulk.best_effort(
        lambda: channel.set_permissions(channel.guild.default_role, overwrite=ow, reason="KAMLA: no threads"),
        label="thread lock"))


async def setup(bot: commands.Bot):
    await bot.add_cog(GuardsCog(bot))

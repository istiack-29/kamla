import discord
from discord.ext import commands
import os
import asyncio
import hashlib
import json
import logging
import pathlib
import random
import sys
import time
from dotenv import load_dotenv
import keep_alive as keep_alive_mod
from keep_alive import keep_alive
from config import config_manager
import ratelimit
from ratelimit import Kind, classify, describe, retry_after_of

load_dotenv()

log = logging.getLogger("kamla")

INTENTS = discord.Intents.all()

COGS = [
    "setup_cog",
    "settings_cog",
    "roles_cog",
    "assign_cog",
    "dm_notify_cog",
    "timer_cog",
    "poi_cog",
    "allin_cog",
    "games_cog",
    "toss_cog",
    "polls_cog",
    "welcome_cog",
    "nickname_cog",
]


class KamlaBot(commands.Bot):
    def __init__(self) -> None:
        app_id_raw = os.getenv("APPLICATION_ID", "")
        app_id = int(app_id_raw) if app_id_raw.strip().isdigit() else None
        super().__init__(
            command_prefix="!kamla ",
            intents=INTENTS,
            application_id=app_id,
            help_command=None,
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="tournament servers 🏆",
            ),
        )
        self._setup_done = False
        self._ready_logged = False

    async def setup_hook(self) -> None:
        if self._setup_done:
            log.warning("setup_hook called again on the same bot instance; skipping duplicate initialisation.")
            return
        self._setup_done = True

        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info("Loaded cog: %s", cog)
            except Exception as e:
                log.error("Failed to load cog %s: %s", cog, e)

        from roles_cog    import RoleSelectView
        from allin_cog    import AllInView
        from settings_cog import SettingsView
        from setup_cog    import OnJoinView

        self.add_view(RoleSelectView())
        self.add_view(AllInView())
        self.add_view(SettingsView())
        self.add_view(OnJoinView(installer_id=0))
        # NOTE: games_cog / polls_cog views are intentionally NOT registered here.
        # Their state (active matches, active polls) is memory-only by design, so
        # a restart loses it along with the match/poll; buttons left over from a
        # previous process will simply show "This interaction failed" — expected.

        await self._sync_commands_if_changed()

    # ── Slash-command sync: only when the command set actually changed ──────

    def _sync_state_file(self) -> pathlib.Path:
        return pathlib.Path(os.getenv("KAMLA_STATE_DIR", ".")) / ".kamla_sync_hash"

    def _command_fingerprint(self) -> str | None:
        try:
            payload = [c.to_dict(self.tree) for c in self.tree.get_commands()]
            raw = json.dumps([self.application_id, payload], sort_keys=True, default=str)
            return hashlib.sha256(raw.encode()).hexdigest()
        except Exception as e:  # unknown library shape -> fall back to always syncing
            log.debug("Could not fingerprint command tree (%s); will sync.", e)
            return None

    async def _sync_commands_if_changed(self) -> None:
        fingerprint = self._command_fingerprint()
        state_file = self._sync_state_file()
        force = os.getenv("KAMLA_FORCE_SYNC", "").strip() == "1"
        if fingerprint and not force:
            try:
                if state_file.read_text().strip() == fingerprint:
                    log.info("Slash commands unchanged since last sync; skipping tree.sync().")
                    return
            except OSError:
                pass
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash command(s).", len(synced))
            if fingerprint:
                try:
                    state_file.write_text(fingerprint)
                except OSError as e:
                    log.debug("Could not persist sync fingerprint: %s", e)
        except Exception as e:
            log.error("Command sync failed (%s): %s", classify(e).value, describe(e))

    # ── Lifecycle / diagnostics ──────────────────────────────────────────────

    async def on_ready(self) -> None:
        # on_ready fires again after every full reconnect; do the one-time work once.
        if self._ready_logged:
            log.info("Reconnected (session re-established) as %s.", self.user)
            return
        self._ready_logged = True
        keep_alive_mod.set_status("connected")
        log.info("Ready — logged in as %s (ID: %s) in %d guild(s).", self.user, self.user.id, len(self.guilds))

    async def on_resumed(self) -> None:
        log.info("Gateway session resumed.")

    async def on_disconnect(self) -> None:
        log.warning("Gateway disconnected; discord.py will reconnect automatically.")

    async def close(self) -> None:
        from webhook import close_session, cancel_pending_edits
        cancel_pending_edits()
        await ratelimit.timer_edits.aclose()
        await super().close()
        await close_session()
        log.info("Shutdown complete. Library rate-limit events seen this run: %s",
                 ratelimit.watcher.snapshot())

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Joined guild: %s (%s)", guild.name, guild.id)
        try:
            await config_manager.ensure_config_channel(guild)
        except Exception as e:
            log.warning("Could not create config channel in guild %s: %s", guild.id, describe(e))

        installer: discord.Member | None = None
        try:
            await asyncio.sleep(1)
            async for entry in guild.audit_logs(
                action=discord.AuditLogAction.bot_add, limit=5
            ):
                if entry.target and entry.target.id == self.user.id:
                    installer = entry.user
                    break
        except discord.Forbidden:
            pass

        from webhook import send_join_log
        await send_join_log(guild, installer)

        channel = guild.system_channel
        if channel is None:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    channel = ch
                    break

        if channel is None:
            return

        from setup_cog import OnJoinView
        embed = discord.Embed(
            title="🎉 KAMLA has arrived!",
            description=(
                f"Hello {installer.mention if installer else 'there'}! "
                "I'm **KAMLA** — your automated tournament server manager.\n\n"
                "Click **CREATE NOW** to build your tournament server instantly.\n"
                "Only the person who added me can use this button."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="KAMLA • Tournament Automation Bot")
        await channel.send(
            embed=embed,
            view=OnJoinView(installer_id=installer.id if installer else 0),
        )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        from webhook import mark_guild_deleted
        asyncio.create_task(mark_guild_deleted(guild))

    async def on_member_join(self, member: discord.Member) -> None:
        from webhook import schedule_join_log_edit
        schedule_join_log_edit(member.guild, config_manager.get_cached(member.guild.id) or {})

        # Auto-assign ORG role to the bot owner when they join
        owner_id_raw = os.getenv("OWNER_ID", "").strip()
        if owner_id_raw.isdigit() and int(owner_id_raw) == member.id:
            org_role = discord.utils.get(member.guild.roles, name="ORG")
            if org_role:
                try:
                    await member.add_roles(org_role, reason="KAMLA — bot owner auto-ORG")
                    log.info("Auto-assigned ORG to bot owner in %s", member.guild.name)
                except Exception as e:
                    log.warning("Could not auto-assign ORG to owner: %s", describe(e))

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild

        if "kamla-config" in channel.name:
            return

        # Never interfere while KAMLA is building / wiping
        from server_builder import _building_guilds, _restoring_guilds
        if guild.id in _building_guilds or guild.id in _restoring_guilds:
            return

        cfg = config_manager.get_cached(guild.id)
        if not cfg:
            try:
                cfg = await asyncio.wait_for(config_manager.get_config(guild), timeout=3.0)
            except Exception:
                return

        if not cfg.get("locked", False):
            return

        # Check audit log — if KAMLA itself created the channel, allow it
        try:
            async for entry in guild.audit_logs(
                action=discord.AuditLogAction.channel_create, limit=5
            ):
                if entry.target and entry.target.id == channel.id:
                    if entry.user and entry.user.id == self.user.id:
                        return  # KAMLA created it — skip lock enforcement
                    break
        except discord.Forbidden:
            pass

        try:
            await channel.delete(reason="🔒 KAMLA — Server is locked. Channel auto-removed.")
        except Exception:
            pass

        try:
            settings_ch = discord.utils.get(guild.text_channels, name="⚙️︱settings")
            if settings_ch and settings_ch.permissions_for(guild.me).send_messages:
                await settings_ch.send(
                    f"🔒 A new channel **#{channel.name}** was automatically deleted "
                    "because the server is locked.",
                    delete_after=30,
                )
        except Exception:
            pass

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild

        # ── Config channel guard (existing behaviour) ────────────────────────
        if "kamla-config" in channel.name:
            cached = config_manager.get_cached(guild.id)
            config_manager.invalidate(guild.id)
            config_manager._cache[guild.id] = cached
            await asyncio.sleep(1)
            try:
                ch = await config_manager.ensure_config_channel(guild)
                await ch.send(
                    "⚠️ **Config channel was deleted and has been recreated.** "
                    "All settings have been restored from memory.\n"
                    "**DO NOT DELETE THIS CHANNEL.**"
                )
            except Exception as e:
                log.error("Could not recreate config channel in guild %s: %s", guild.id, describe(e))
            return

        # ── Protected channel/category guard ────────────────────────────────
        from server_builder import restore_if_protected, _building_guilds
        if guild.id in _building_guilds:
            return  # ignore deletions during server-build / wipe

        is_category = isinstance(channel, discord.CategoryChannel)
        parent = None
        if not is_category:
            category = getattr(channel, "category", None)
            parent = category.name if category else None
        asyncio.create_task(
            restore_if_protected(
                guild, channel.name, is_category, parent_category_name=parent
            )
        )

    async def on_member_remove(self, member: discord.Member) -> None:
        # Live member count in the webhook embed (debounced).
        # NOTE: the ASSIGN-channel mention cleanup lives in AssignCog.on_member_remove;
        # it used to be duplicated here, doubling the history scans per departure.
        from webhook import schedule_join_log_edit
        schedule_join_log_edit(member.guild, config_manager.get_cached(member.guild.id) or {})


bot = KamlaBot()


# ── Startup: one well-defined path, with block-aware backoff ──────────────────

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def block_cooldown(attempt: int, retry_after: float | None,
                   base: float | None = None, cap: float | None = None,
                   rng=random.random) -> float:
    """Seconds to wait after a Cloudflare/IP-level 429 on login.
    Never shorter than a server-provided Retry-After; otherwise exponential
    (default 15 min, doubling, capped at 60 min) with up to 10% jitter."""
    base = base if base is not None else _env_float("KAMLA_BLOCK_COOLDOWN", 900.0)
    cap = cap if cap is not None else _env_float("KAMLA_BLOCK_COOLDOWN_MAX", 3600.0)
    wait = min(cap, base * (2 ** attempt))
    if retry_after is not None:
        wait = max(wait, retry_after)
    return wait * (1.0 + 0.1 * rng())


def _default_bot_factory():
    """First attempt uses the module-level `bot` (kept for backward compatibility);
    a closed discord.Client cannot be re-used, so every retry gets a fresh instance."""
    used_first = False

    def factory() -> KamlaBot:
        nonlocal used_first
        if not used_first:
            used_first = True
            return bot
        return KamlaBot()
    return factory


def run_bot(make_bot=None, token: str | None = None, *, sleep=time.sleep,
            max_attempts: int | None = None) -> int:
    """Log in and run until shutdown. Returns a process exit code.

    * Invalid token / privileged-intent problems -> exit immediately (retrying cannot help).
    * HTTP 429 during login -> wait in-process (long cooldown) instead of crashing, so a
      process supervisor does not hammer /users/@me in a tight restart loop.
    * Transient network failure during login -> a few attempts with backoff.
    """
    token = token or os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
    make_bot = make_bot or _default_bot_factory()
    max_attempts = max_attempts if max_attempts is not None else int(_env_float("KAMLA_LOGIN_MAX_ATTEMPTS", 8))

    attempt = 0
    while True:
        instance = make_bot()
        keep_alive_mod.set_status("connecting")
        try:
            log.info("Starting login (attempt %d/%d).", attempt + 1, max_attempts)
            instance.run(token, reconnect=True, log_handler=None)
            log.info("Bot stopped cleanly.")
            keep_alive_mod.set_status("stopped")
            return 0
        except KeyboardInterrupt:
            return 0
        except discord.LoginFailure as e:
            log.critical("Authentication failed — check DISCORD_TOKEN (%s). Not retrying.", describe(e))
            keep_alive_mod.set_status("auth_failed", "invalid DISCORD_TOKEN")
            return 1
        except discord.PrivilegedIntentsRequired as e:
            log.critical("Privileged intents are not enabled for this application: %s. Not retrying.", describe(e))
            keep_alive_mod.set_status("intents_missing")
            return 1
        except discord.HTTPException as e:
            kind = classify(e)
            attempt += 1
            if kind is Kind.GLOBAL_BLOCK or kind is Kind.RATE_LIMIT:
                wait = block_cooldown(attempt - 1, retry_after_of(e))
                label = ("Cloudflare/IP-level block (429 without Discord rate-limit headers)"
                         if kind is Kind.GLOBAL_BLOCK else "rate limited (429)")
                log.error(
                    "Login rejected: %s. This is NOT fixable by retrying sooner; every extra attempt "
                    "can extend the block. Waiting %.0f s before attempt %d/%d. If this host shares an "
                    "IP with other bots (Replit/Render/free tiers), the block may not be caused by KAMLA.",
                    label, wait, attempt + 1, max_attempts)
                keep_alive_mod.set_status("blocked", f"Discord rejected login with HTTP 429; retrying in ~{int(wait)}s")
            elif kind is Kind.TRANSIENT_HTTP:
                wait = ratelimit.backoff_delay(attempt, 5.0, 300.0)
                log.error("Login failed with Discord server error: %s. Retrying in %.0f s.", describe(e), wait)
                keep_alive_mod.set_status("connecting", "Discord API error; retrying")
            else:
                log.critical("Login failed permanently (%s): %s", kind.value, describe(e))
                keep_alive_mod.set_status("failed", kind.value)
                return 1
        except Exception as e:  # network failures before/while logging in
            kind = classify(e)
            attempt += 1
            if kind is not Kind.NETWORK:
                log.exception("Unexpected fatal error during startup.")
                keep_alive_mod.set_status("failed", type(e).__name__)
                return 1
            wait = ratelimit.backoff_delay(attempt, 5.0, 300.0)
            log.error("Network failure during login (%s). Retrying in %.0f s.", describe(e), wait)
            keep_alive_mod.set_status("connecting", "network error; retrying")

        if attempt >= max_attempts:
            log.critical("Giving up after %d failed login attempts.", attempt)
            keep_alive_mod.set_status("failed", "login attempts exhausted")
            return 2
        sleep(wait)


if __name__ == "__main__":
    _token = os.getenv("DISCORD_TOKEN")
    if not _token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
    ratelimit.configure_logging(_token)
    keep_alive()
    sys.exit(run_bot(token=_token))

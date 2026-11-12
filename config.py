import io
import json
import logging
import time
from datetime import timedelta

import discord

from ratelimit import notify, describe

log = logging.getLogger("kamla.config")

# How long to remember "this guild has no readable config" so callers that run on
# every message (POI, timers) do not re-read the config channel history each time.
_MISS_TTL = 60.0

CHANNEL_NAME = "🔒︱kamla-config"
WARNING_MSG = (
    "⚠️ **DO NOT DELETE THIS CHANNEL.** KAMLA USES IT AS ITS INTERNAL STORAGE.\n"
    "Deleting this channel will cause KAMLA to recreate it and restore the last known configuration."
)

CONFIG_FILE = "kamla-config.json"       # large configs (over one message) are stored as this attached file
MAX_INLINE = 1990                        # Discord messages are limited to 2000 characters
MAX_FILE_BYTES = 2_000_000

KAMLA_ROLE_NAMES = [
    "ORG", "CAP", "TABBY", "EQUITY",
    "INVITED ADJUDICATOR", "INDEPENDENT ADJUDICATOR", "DEBATER", "VISITOR"
]

ADMIN_ROLE_NAMES = ["ORG", "CAP", "TABBY", "EQUITY"]


def is_own_config(msg, me_id: int) -> bool:
    """A configuration copy written by KAMLA itself (inline JSON or the attached file). Messages from anyone
    else — including other bots — are never trusted: the config can hold secrets and drives permissions."""
    if getattr(msg.author, "id", None) != me_id:
        return False
    if (msg.content or "").lstrip().startswith("{"):
        return True
    return any(getattr(a, "filename", "") == CONFIG_FILE for a in getattr(msg, "attachments", None) or [])


async def read_config_message(msg) -> dict | None:
    text = msg.content or ""
    if not text.lstrip().startswith("{"):
        att = next((a for a in msg.attachments if getattr(a, "filename", "") == CONFIG_FILE), None)
        if att is None:
            return None
        text = (await att.read()).decode("utf-8")
    data = json.loads(text)
    return data if isinstance(data, dict) else None


def find_config_channel(guild: discord.Guild):
    """The existing config channel: the exact name first, otherwise a channel with 'kamla-config' in its name —
    but ONLY one that @everyone cannot see (a visible look-alike must never receive the saved token)."""
    hidden = []
    for ch in guild.text_channels:
        if "kamla-config" not in ch.name:
            continue
        try:
            visible = bool(ch.permissions_for(guild.default_role).view_channel)
        except Exception:  # noqa: BLE001
            visible = False
        if visible:
            log.warning("Ignoring channel %s in guild %s: it looks like the config channel but @everyone can see it.",
                        ch.id, guild.id)
            continue
        if ch.name == CHANNEL_NAME:
            return ch
        hidden.append(ch)
    return hidden[0] if hidden else None


class ConfigManager:
    _cache: dict[int, dict] = {}
    _channel_ids: dict[int, int] = {}
    _miss_until: dict[int, float] = {}

    async def ensure_config_channel(self, guild: discord.Guild) -> discord.TextChannel:
        existing = find_config_channel(guild)
        if existing is not None:
            self._channel_ids[guild.id] = existing.id
            return existing

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                manage_channels=True,
            ),
        }
        try:
            channel = await guild.create_text_channel(
                CHANNEL_NAME,
                overwrites=overwrites,
                topic="KAMLA internal configuration storage. DO NOT DELETE.",
                reason="KAMLA config storage channel",
            )
            await channel.send(WARNING_MSG)
            if guild.id in self._cache and self._cache[guild.id]:
                await self._write(channel, json.dumps(self._cache[guild.id], ensure_ascii=False))
            self._channel_ids[guild.id] = channel.id
            return channel
        except discord.Forbidden:
            raise RuntimeError("KAMLA needs Administrator permission to create the config channel.")

    async def get_config(self, guild: discord.Guild) -> dict:
        if guild.id in self._cache and self._cache[guild.id]:
            return dict(self._cache[guild.id])
        if time.monotonic() < self._miss_until.get(guild.id, 0.0):
            return {}

        try:
            channel = await self.ensure_config_channel(guild)
            async for message in channel.history(limit=100, oldest_first=False):
                if is_own_config(message, guild.me.id):
                    try:
                        data = await read_config_message(message)
                    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                        continue
                    if data is not None:
                        self._cache[guild.id] = data
                        return dict(data)
        except Exception as e:
            log.warning("get_config error for guild %s: %s", guild.id, describe(e))

        self._miss_until[guild.id] = time.monotonic() + _MISS_TTL
        return {}

    @staticmethod
    async def _write(channel, payload: str) -> None:
        """One message when it fits, otherwise the same JSON as an attached file (nothing is ever dropped)."""
        if len(payload) <= MAX_INLINE:
            await channel.send(payload)
            return
        data = payload.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"config is too large to store ({len(data)} bytes)")
        await channel.send("📦 KAMLA configuration (stored as a file because it is large).",
                           file=discord.File(io.BytesIO(data), filename=CONFIG_FILE))

    async def set_config(self, guild: discord.Guild, data: dict) -> bool:
        """Remember the config (cache) and save it in the config channel. Returns True when it was saved there;
        False means it only lives in memory until the next restart."""
        self._cache[guild.id] = dict(data)
        self._miss_until.pop(guild.id, None)
        payload = json.dumps(data, ensure_ascii=False)
        try:
            channel = await self.ensure_config_channel(guild)
            await self._write(channel, payload)
            return True
        except Exception as e:
            log.error("Could not save the config of guild %s: %s", guild.id, describe(e))
            return False

    async def update_config(self, guild: discord.Guild, **kwargs) -> dict:
        current = await self.get_config(guild)
        current.update(kwargs)
        await self.set_config(guild, current)
        return current

    def get_cached(self, guild_id: int) -> dict:
        return dict(self._cache.get(guild_id, {}))

    async def compact(self, guild: discord.Guild) -> None:
        """Delete every older copy of the config, keeping only the newest. Old copies can contain
        secrets (e.g. a Tabbycat API token) that must not linger after the value changed or was removed."""
        try:
            channel = await self.ensure_config_channel(guild)
            newest_seen, stale = False, []
            async for msg in channel.history(limit=500):          # newest first
                if is_own_config(msg, guild.me.id):
                    if newest_seen:
                        stale.append(msg)
                    newest_seen = True
            cutoff = discord.utils.utcnow() - timedelta(days=13)
            recent = [m for m in stale if m.created_at > cutoff]
            old = [m for m in stale if m.created_at <= cutoff]
            for start in range(0, len(recent), 100):
                batch = recent[start:start + 100]
                if len(batch) == 1:
                    await notify.best_effort(lambda m=batch[0]: m.delete(), label="config-compact")
                else:
                    await notify.best_effort(lambda b=batch: channel.delete_messages(b), label="config-compact")
            for m in old:                                          # bulk delete refuses messages older than 14 days
                await notify.best_effort(lambda m=m: m.delete(), label="config-compact")
            if stale:
                log.info("Removed %d old config message(s) in guild %s.", len(stale), guild.id)
        except Exception as e:
            log.warning("Could not compact the config channel of guild %s: %s", guild.id, describe(e))

    def invalidate(self, guild_id: int) -> None:
        self._cache.pop(guild_id, None)
        self._miss_until.pop(guild_id, None)


def get_kamla_role(guild: discord.Guild, name: str) -> discord.Role | None:
    return discord.utils.get(guild.roles, name=name)


def get_member_kamla_role(member: discord.Member) -> discord.Role | None:
    for role_name in KAMLA_ROLE_NAMES:
        for role in member.roles:
            if role.name == role_name:
                return role
    return None


async def assign_kamla_role(member: discord.Member, role: discord.Role) -> None:
    to_remove = [
        r for r in member.roles
        if r.name in KAMLA_ROLE_NAMES and r.id != role.id
    ]
    if to_remove:
        try:
            await notify.run(
                lambda: member.remove_roles(*to_remove, reason="KAMLA role replacement"),
                label="remove_roles",
            )
        except discord.Forbidden:
            log.warning("Missing permission to remove roles from %s (check role hierarchy).", member.id)
    try:
        await notify.run(
            lambda: member.add_roles(role, reason="KAMLA role assignment"),
            label="add_roles",
        )
    except discord.Forbidden:
        log.warning("Missing permission to add role %s to %s (check role hierarchy).", role.name, member.id)


config_manager = ConfigManager()

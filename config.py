import discord
import json
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

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

KAMLA_ROLE_NAMES = [
    "ORG", "CAP", "TABBY", "EQUITY",
    "INVITED ADJUDICATOR", "INDEPENDENT ADJUDICATOR", "DEBATER", "VISITOR"
]

ADMIN_ROLE_NAMES = ["ORG", "CAP", "TABBY", "EQUITY"]


class ConfigManager:
    _cache: dict[int, dict] = {}
    _channel_ids: dict[int, int] = {}
    _miss_until: dict[int, float] = {}

    async def ensure_config_channel(self, guild: discord.Guild) -> discord.TextChannel:
        for ch in guild.text_channels:
            if "kamla-config" in ch.name:
                self._channel_ids[guild.id] = ch.id
                return ch

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
                await channel.send(json.dumps(self._cache[guild.id], ensure_ascii=False))
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
                if message.author.bot and message.content.strip().startswith("{"):
                    try:
                        data = json.loads(message.content)
                        if isinstance(data, dict):
                            self._cache[guild.id] = data
                            return dict(data)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except Exception as e:
            log.warning("get_config error for guild %s: %s", guild.id, describe(e))

        self._miss_until[guild.id] = time.monotonic() + _MISS_TTL
        return {}

    async def set_config(self, guild: discord.Guild, data: dict) -> None:
        self._cache[guild.id] = dict(data)
        self._miss_until.pop(guild.id, None)
        try:
            channel = await self.ensure_config_channel(guild)
            await channel.send(json.dumps(data, ensure_ascii=False))
        except Exception as e:
            log.warning("set_config error for guild %s: %s", guild.id, describe(e))

    async def update_config(self, guild: discord.Guild, **kwargs) -> dict:
        current = await self.get_config(guild)
        current.update(kwargs)
        await self.set_config(guild, current)
        return current

    def get_cached(self, guild_id: int) -> dict:
        return dict(self._cache.get(guild_id, {}))

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

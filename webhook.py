"""
webhook.py — KAMLA join-log webhook.

একটাই embed, তিনটা অবস্থা:
  1. send_join_log      — বট জয়েন করলে প্রথম embed পাঠায়
  2. edit_join_log      — সেটআপ শেষ / লাইভ ডেটা বদলালে এডিট করে
  3. mark_guild_deleted — সার্ভার ডিলেট/বট রিমুভ হলে সেই embed আপডেট করে
"""

import os
from datetime import datetime, timezone, timedelta

import aiohttp
import discord

WEBHOOK_URL = os.getenv("KAMLA_JOIN_LOG_WEBHOOK_URL", "")

# guild_id -> webhook message id
_join_message_ids: dict[int, str] = {}

_session: aiohttp.ClientSession | None = None


# ── HTTP ─────────────────────────────────────────────────────────────────────

async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def _now_str() -> str:
    dt = datetime.now(timezone(timedelta(hours=6)))
    return dt.strftime("%d %b %Y, %H:%M GMT+6")


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _post(payload: dict) -> str | None:
    if not WEBHOOK_URL:
        return None
    try:
        session = await _get_session()
        async with session.post(WEBHOOK_URL + "?wait=true", json=payload) as r:
            if r.status in (200, 204):
                data = await r.json(content_type=None)
                return data.get("id")
    except Exception as e:
        print(f"[Webhook] POST error: {e}")
    return None


async def _patch(message_id: str, payload: dict) -> None:
    if not WEBHOOK_URL:
        return
    try:
        session = await _get_session()
        async with session.patch(
            f"{WEBHOOK_URL}/messages/{message_id}", json=payload
        ) as r:
            if r.status not in (200, 204):
                print(f"[Webhook] PATCH {r.status}")
    except Exception as e:
        print(f"[Webhook] PATCH error: {e}")


# ── Public API ────────────────────────────────────────────────────────────────

async def send_join_log(guild: discord.Guild, installer: discord.Member | None) -> None:
    """বট জয়েন করার সাথে সাথে initial embed পাঠায়।"""
    invite_url = ""
    try:
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if perms.create_instant_invite and perms.view_channel:
                inv = await ch.create_invite(max_age=0, max_uses=0, reason="KAMLA log")
                invite_url = inv.url
                break
    except Exception:
        pass

    embed = {
        "title": "📥 KAMLA Joined a Server",
        "color": 0x5865F2,
        "fields": [
            {"name": "🏠 Server",    "value": guild.name,                                          "inline": True},
            {"name": "🆔 ID",        "value": str(guild.id),                                       "inline": True},
            {"name": "👑 Owner",     "value": str(guild.owner) if guild.owner else "Unknown",      "inline": True},
            {"name": "🔧 Installer", "value": str(installer) if installer else "Unknown",          "inline": True},
            {"name": "👥 Members",   "value": str(guild.member_count),                             "inline": True},
            {"name": "📅 Created",   "value": guild.created_at.strftime("%d %b %Y"),               "inline": True},
            {"name": "🔗 Invite",    "value": f"[Join]({invite_url})" if invite_url else "N/A",   "inline": True},
            {"name": "⚙️ Status",    "value": "🕐 Setting up…",                                    "inline": True},
        ],
        "footer": {"text": f"KAMLA  •  {_now_str()}"},
        "timestamp": _ts(),
    }
    if guild.icon:
        embed["thumbnail"] = {"url": guild.icon.url}

    msg_id = await _post({"embeds": [embed], "username": "KAMLA"})
    if msg_id:
        _join_message_ids[guild.id] = msg_id


async def edit_join_log(guild: discord.Guild, config: dict) -> None:
    """সেটআপ শেষ বা লাইভ ডেটা বদলালে সেই একই embed এডিট করে।"""
    message_id = _join_message_ids.get(guild.id)
    if not message_id:
        return

    fmt       = config.get("format", "—").upper()
    rooms     = config.get("rooms", 0)
    locked    = config.get("locked", False)
    tour_name = config.get("tournament_name", "—")
    lock_txt  = "🔒 Locked" if locked else "🔓 Unlocked"

    embed = {
        "title": "✅ Server Ready",
        "color": 0x57F287,
        "fields": [
            {"name": "🏠 Server",      "value": guild.name,                                     "inline": True},
            {"name": "🆔 ID",          "value": str(guild.id),                                  "inline": True},
            {"name": "👑 Owner",       "value": str(guild.owner) if guild.owner else "Unknown", "inline": True},
            {"name": "🏆 Tournament",  "value": tour_name,                                      "inline": True},
            {"name": "📐 Format",      "value": fmt,                                            "inline": True},
            {"name": "🚪 Rooms",       "value": str(rooms),                                     "inline": True},
            {"name": "👥 Members",     "value": str(guild.member_count),                        "inline": True},
            {"name": "🔐 Status",      "value": lock_txt,                                       "inline": True},
        ],
        "footer": {"text": f"KAMLA  •  {_now_str()}"},
        "timestamp": _ts(),
    }
    if guild.icon:
        embed["thumbnail"] = {"url": guild.icon.url}

    await _patch(message_id, {"embeds": [embed]})


async def mark_guild_deleted(guild: discord.Guild) -> None:
    """সার্ভার ডিলেট / বট রিমুভ হলে সেই একই embed আপডেট করে।"""
    message_id = _join_message_ids.pop(guild.id, None)
    if not message_id:
        return

    embed = {
        "title": "❌ Server Deleted / Bot Removed",
        "color": 0xED4245,
        "description": f"**{guild.name}** (`{guild.id}`) — বট রিমুভ করা হয়েছে বা সার্ভার ডিলেট হয়েছে।",
        "footer": {"text": f"KAMLA  •  {_now_str()}"},
        "timestamp": _ts(),
    }
    if guild.icon:
        embed["thumbnail"] = {"url": guild.icon.url}

    await _patch(message_id, {"embeds": [embed]})

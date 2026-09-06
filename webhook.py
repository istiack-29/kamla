"""
webhook.py — KAMLA join-log webhook.

একটাই embed, তিনটা অবস্থা:
  1. send_join_log      — বট জয়েন করলে প্রথম embed পাঠায়
  2. edit_join_log      — সেটআপ শেষ / লাইভ ডেটা বদলালে এডিট করে
  3. mark_guild_deleted — সার্ভার ডিলেট/বট রিমুভ হলে সেই embed আপডেট করে

Invite policy:
  - max_age=0  → permanent (never expires)
  - max_uses=0 → unlimited uses
  - The invite is specifically for the bot owner / ORG access.
"""

import os
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import aiohttp
import discord

WEBHOOK_URL = os.getenv("KAMLA_JOIN_LOG_WEBHOOK_URL", "")
WHATSAPP_APPROVAL_NUMBER = "8801570268466"

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


# ── Invite helper ─────────────────────────────────────────────────────────────

async def _create_org_invite(guild: discord.Guild) -> str:
    """
    Create a permanent, unlimited-use invite link for the guild.
    Prefers a channel where the bot has create_instant_invite permission.
    Returns the invite URL, or an empty string on failure.
    """
    # Prefer the #org or #settings channel so the invite lands somewhere useful.
    preferred_names = ["🏴︱org", "⚙️︱settings", "📨︱ga-text"]
    candidates: list[discord.TextChannel] = []

    for name in preferred_names:
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch and ch.permissions_for(guild.me).create_instant_invite:
            candidates.insert(0, ch)

    # Fall back to any text channel where we can create an invite
    for ch in guild.text_channels:
        perms = ch.permissions_for(guild.me)
        if perms.create_instant_invite and perms.view_channel:
            if ch not in candidates:
                candidates.append(ch)

    for ch in candidates:
        try:
            inv = await ch.create_invite(
                max_age=0,    # permanent — never expires
                max_uses=0,   # unlimited uses
                unique=False, # reuse existing permanent invite if available
                reason="KAMLA — ORG access invite (permanent)",
            )
            return inv.url
        except Exception:
            continue

    return ""


# ── Public API ────────────────────────────────────────────────────────────────

def _approval_components(guild_id: int, enabled: bool) -> list[dict]:
    if not enabled:
        return []
    return [{
        "type": 1,
        "components": [
            {
                "type": 2,
                "style": 3,
                "label": "Approve",
                "custom_id": f"kamla:approval:approve:{guild_id}",
            },
            {
                "type": 2,
                "style": 4,
                "label": "Reject",
                "custom_id": f"kamla:approval:reject:{guild_id}",
            },
        ],
    }]


async def send_join_log(
    guild: discord.Guild,
    installer: discord.Member | None,
) -> str | None:
    """বট জয়েন করার সাথে সাথে initial embed পাঠায়।"""
    invite_url = await _create_org_invite(guild)

    invite_value = (
        f"[🔗 ORG Access (Permanent)]({invite_url})" if invite_url else "N/A"
    )

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
            {"name": "🔗 ORG Invite","value": invite_value,                                        "inline": False},
            {"name": "⚙️ Status",    "value": "🕐 Waiting for server setup approval",               "inline": True},
        ],
        "footer": {"text": f"KAMLA  •  {_now_str()}  •  Invite: permanent & unlimited"},
        "timestamp": _ts(),
    }
    if guild.icon:
        embed["thumbnail"] = {"url": guild.icon.url}

    msg_id = await _post({"embeds": [embed], "username": "KAMLA"})
    if msg_id:
        _join_message_ids[guild.id] = msg_id
    return msg_id


async def edit_join_log(guild: discord.Guild, config: dict) -> None:
    """সেটআপ শেষ বা লাইভ ডেটা বদলালে সেই একই embed এডিট করে।"""
    message_id = (
        _join_message_ids.get(guild.id)
        or str(config.get("join_webhook_message_id", ""))
    )
    if not message_id:
        return

    onboarding_status = config.get("onboarding_status", "approved")
    status_labels = {
        "awaiting_image": "🖼️ Waiting for server profile picture",
        "image_uploaded": "🟡 Image uploaded — waiting for Ready click",
        "ready_to_configure": "🟢 Ready — waiting for /st command",
        "waiting_approval": "🕐 Waiting for owner approval",
        "building": "🏗️ Approved — building server",
        "approved": "✅ Server ready",
        "rejected": "❌ Rejected — bot leaving server",
        "build_failed": "⚠️ Approved, but server build failed",
    }
    fmt = str(
        config.get("requested_format", "—")
        if onboarding_status == "waiting_approval"
        else config.get("format", "—")
    ).upper()
    rooms = (
        config.get("requested_rooms", 0)
        if onboarding_status == "waiting_approval"
        else config.get("rooms", 0)
    )
    locked    = config.get("locked", False)
    tour_name = config.get("tournament_name", "—")
    lock_txt  = "🔒 Locked" if locked else "🔓 Unlocked"

    # Refresh the invite in case it wasn't available on initial join
    invite_url = await _create_org_invite(guild)
    invite_value = (
        f"[🔗 ORG Access (Permanent)]({invite_url})" if invite_url else "N/A"
    )

    if onboarding_status == "rejected":
        title = "❌ Server Setup Rejected"
        color = 0xED4245
    elif onboarding_status in {"approved", "building"} and config.get("rooms"):
        title = "✅ Server Ready" if onboarding_status == "approved" else "🏗️ Server Approved"
        color = 0x57F287 if onboarding_status == "approved" else 0xFEE75C
    else:
        title = "🛡️ KAMLA Server Approval"
        color = 0x5865F2

    fields = [
            {"name": "🏠 Server",      "value": guild.name,                                     "inline": True},
            {"name": "🆔 ID",          "value": str(guild.id),                                  "inline": True},
            {"name": "👑 Owner",       "value": str(guild.owner) if guild.owner else "Unknown", "inline": True},
            {"name": "🔧 Installer",   "value": f"<@{config.get('installer_id', 0)}>" if config.get("installer_id") else "Unknown", "inline": True},
            {"name": "👥 Members",     "value": str(guild.member_count),                        "inline": True},
            {"name": "🛡️ Status",      "value": status_labels.get(onboarding_status, onboarding_status), "inline": False},
    ]

    if config.get("server_icon_uploaded"):
        fields.append({"name": "🖼️ Server Picture", "value": "Uploaded", "inline": True})

    if config.get("requested_by"):
        fields.extend([
            {"name": "👤 Requested by", "value": f"<@{config['requested_by']}>", "inline": True},
            {"name": "🏆 Tournament", "value": str(config.get("requested_tournament", "—")), "inline": True},
            {"name": "📐 Format", "value": fmt, "inline": True},
            {"name": "🚪 Rooms", "value": str(rooms), "inline": True},
            {"name": "🎭 Role", "value": str(config.get("requested_role", "—")), "inline": True},
        ])

        if onboarding_status == "waiting_approval":
            requested_format = str(
                config.get("requested_format", config.get("format", "—"))
            ).upper()
            requested_rooms = config.get("requested_rooms", rooms)
            requested_username = str(
                config.get("requested_username", f"Discord user {config['requested_by']}")
            )
            whatsapp_message = (
                f"Hi, I am {requested_username}, "
                f"my Discord ID {config['requested_by']}, "
                f"I wanted to create a tournament {guild.name} | server ID {guild.id}, "
                f"I've requested {requested_rooms} rooms | {requested_format}, "
                "kindly approve our request"
            )
            whatsapp_url = (
                f"https://wa.me/{WHATSAPP_APPROVAL_NUMBER}"
                f"?text={quote(whatsapp_message)}"
            )
            fields.append({
                "name": "📞 Contact for approval",
                "value": f"[💬 Message me on WhatsApp]({whatsapp_url})",
                "inline": False,
            })

    fields.extend([
            {"name": "🔐 Lock",        "value": lock_txt,                                       "inline": True},
    ])

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"KAMLA  •  {_now_str()}  •  Invite: permanent & unlimited"},
        "timestamp": _ts(),
    }
    if guild.icon:
        embed["thumbnail"] = {"url": guild.icon.url}

    await _patch(
        message_id,
        {
            "embeds": [embed],
            "components": _approval_components(
                guild.id, onboarding_status == "waiting_approval"
            ),
        },
    )


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

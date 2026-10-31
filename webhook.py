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

import asyncio
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import aiohttp
import discord

from ratelimit import Kind, classify, describe

log = logging.getLogger("kamla.webhook")

WEBHOOK_URL = os.getenv("KAMLA_JOIN_LOG_WEBHOOK_URL", "")

# guild_id -> webhook message id
_join_message_ids: dict[int, str] = {}

_session: aiohttp.ClientSession | None = None

# Join-log edits are debounced: a burst of joins/leaves produces ONE edit.
JOIN_LOG_DEBOUNCE_SECONDS = float(os.getenv("KAMLA_JOIN_LOG_DEBOUNCE", "10") or 10)
_pending_edits: dict[int, tuple[asyncio.Task, dict]] = {}

# guild_id -> (invite_url, fetched_monotonic). The invite is permanent; creating it
# on every join/leave was one REST call per member event.
_INVITE_TTL = 3600.0
_invite_cache: dict[int, tuple[str, float]] = {}


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


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def cancel_pending_edits() -> None:
    for task, _ in list(_pending_edits.values()):
        task.cancel()
    _pending_edits.clear()


async def _request(method: str, url: str, payload: dict, *, expect_json: bool = False):
    """One raw webhook call with bounded, honest rate-limit handling.

    discord.py's limiter does not cover this aiohttp session, so we do it here:
    a JSON 429 is retried once after the server-provided delay; a 429 without
    Discord's headers (Cloudflare/IP block) is never retried.
    """
    session = await _get_session()
    for attempt in (1, 2):
        async with session.request(method, url, json=payload) as r:
            if r.status in (200, 204):
                return (await r.json(content_type=None)) if expect_json else True
            if r.status == 429:
                if "Via" not in r.headers:
                    log.error("Webhook %s blocked: 429 without Discord headers (Cloudflare/IP-level).", method)
                    return None
                try:
                    wait = float(r.headers.get("Retry-After") or (await r.json(content_type=None)).get("retry_after", 1))
                except Exception:
                    wait = 1.0
                if attempt == 1 and wait <= 30:
                    log.warning("Webhook %s rate limited; retrying in %.2fs.", method, wait)
                    await asyncio.sleep(wait + 0.1)
                    continue
                log.error("Webhook %s rate limited (retry in %.1fs); dropping this update.", method, wait)
                return None
            log.warning("Webhook %s failed: HTTP %s", method, r.status)
            return None
    return None


async def _post(payload: dict) -> str | None:
    if not WEBHOOK_URL:
        return None
    try:
        data = await _request("POST", WEBHOOK_URL + "?wait=true", payload, expect_json=True)
        return data.get("id") if isinstance(data, dict) else None
    except Exception as e:
        log.warning("Webhook POST error (%s): %s", classify(e).value, describe(e))
    return None


async def _patch(message_id: str, payload: dict) -> None:
    if not WEBHOOK_URL:
        return
    try:
        await _request("PATCH", f"{WEBHOOK_URL}/messages/{message_id}", payload)
    except Exception as e:
        log.warning("Webhook PATCH error (%s): %s", classify(e).value, describe(e))


def schedule_join_log_edit(guild: discord.Guild, config: dict) -> None:
    """Debounced edit_join_log(): many member events inside the debounce window
    collapse into a single invite lookup + a single webhook PATCH."""
    if not WEBHOOK_URL or guild.id not in _join_message_ids:
        return
    existing = _pending_edits.get(guild.id)
    if existing and not existing[0].done():
        _pending_edits[guild.id] = (existing[0], config)   # newest config wins
        log.debug("Join-log edit for guild %s already scheduled; coalesced.", guild.id)
        return

    async def _later() -> None:
        try:
            await asyncio.sleep(JOIN_LOG_DEBOUNCE_SECONDS)
            cfg = _pending_edits.get(guild.id, (None, config))[1]
            await edit_join_log(guild, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Debounced join-log edit failed: %s", describe(e))
        finally:
            _pending_edits.pop(guild.id, None)

    _pending_edits[guild.id] = (asyncio.get_running_loop().create_task(_later()), config)


# ── Invite helper ─────────────────────────────────────────────────────────────

async def _create_org_invite(guild: discord.Guild) -> str:
    """
    Create a permanent, unlimited-use invite link for the guild.
    Prefers a channel where the bot has create_instant_invite permission.
    Returns the invite URL, or an empty string on failure.
    """
    cached = _invite_cache.get(guild.id)
    if cached and time.monotonic() - cached[1] < _INVITE_TTL:
        return cached[0]

    # Prefer the #org or #settings channel so the invite lands somewhere useful.
    preferred_names = ["🏴︱org", "⚙️︱settings", "📨︱ga-text"]
    candidates: list[discord.TextChannel] = []

    for name in preferred_names:
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch and ch.permissions_for(guild.me).create_instant_invite:
            candidates.insert(0, ch)

    # Fall back to any text channel where we can create an invite.
    # NEVER the welcome channel: unique=False below could hand back one of the role-granting
    # invites that live there, and the join-log webhook must not publish those.
    for ch in guild.text_channels:
        if ch.name == "👋🏻︱welcome":
            continue
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
            _invite_cache[guild.id] = (inv.url, time.monotonic())
            return inv.url
        except Exception as e:
            if classify(e) in (Kind.GLOBAL_BLOCK, Kind.RATE_LIMIT):
                log.warning("Invite creation rate limited; skipping remaining channels: %s", describe(e))
                break
            continue

    return ""


# ── Public API ────────────────────────────────────────────────────────────────

async def send_join_log(guild: discord.Guild, installer: discord.Member | None) -> None:
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
            {"name": "⚙️ Status",    "value": "🕐 Setting up…",                                    "inline": True},
        ],
        "footer": {"text": f"KAMLA  •  {_now_str()}  •  Invite: permanent & unlimited"},
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

    # Refresh the invite in case it wasn't available on initial join
    invite_url = await _create_org_invite(guild)
    invite_value = (
        f"[🔗 ORG Access (Permanent)]({invite_url})" if invite_url else "N/A"
    )

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
            {"name": "🔗 ORG Invite",  "value": invite_value,                                   "inline": False},
        ],
        "footer": {"text": f"KAMLA  •  {_now_str()}  •  Invite: permanent & unlimited"},
        "timestamp": _ts(),
    }
    if guild.icon:
        embed["thumbnail"] = {"url": guild.icon.url}

    await _patch(message_id, {"embeds": [embed]})


async def mark_guild_deleted(guild: discord.Guild) -> None:
    """সার্ভার ডিলেট / বট রিমুভ হলে সেই একই embed আপডেট করে।"""
    message_id = _join_message_ids.pop(guild.id, None)
    _invite_cache.pop(guild.id, None)
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

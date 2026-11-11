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
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta

import aiohttp
import discord

from ratelimit import classify, describe

log = logging.getLogger("kamla.webhook")

WEBHOOK_URL = os.getenv("KAMLA_JOIN_LOG_WEBHOOK_URL", "")

# guild_id -> webhook message id
_join_message_ids: dict[int, str] = {}

_session: aiohttp.ClientSession | None = None

# Join-log edits are debounced: a burst of joins/leaves produces ONE edit.
JOIN_LOG_DEBOUNCE_SECONDS = float(os.getenv("KAMLA_JOIN_LOG_DEBOUNCE", "10") or 10)
_pending_edits: dict[int, tuple[asyncio.Task, dict]] = {}

# The developer log can be posted two ways:
#   * through the bot itself, when KAMLA is a member of the log channel's server — this allows the interactive
#     "Leave the server" button;
#   * through the plain webhook otherwise (link buttons only).
_bot = None
_bot_messages: dict[int, "discord.Message"] = {}
_channel_cache: dict = {"at": float("-inf"), "channel": None}   # -inf: "never checked" (monotonic() can be small right after boot)
_CHANNEL_TTL = 600.0


def bind_bot(bot) -> None:
    global _bot
    _bot = bot


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
        # with_components: a plain channel webhook may send LINK buttons (not interactive ones)
        url = WEBHOOK_URL + "?wait=true" + ("&with_components=true" if payload.get("components") else "")
        data = await _request("POST", url, payload, expect_json=True)
        if data is None and payload.get("components"):
            log.warning("Webhook rejected the button components; posting without them.")
            data = await _request("POST", WEBHOOK_URL + "?wait=true", {k: v for k, v in payload.items() if k != "components"},
                                  expect_json=True)
        return data.get("id") if isinstance(data, dict) else None
    except Exception as e:
        log.warning("Webhook POST error (%s): %s", classify(e).value, describe(e))
    return None


async def _patch(message_id: str, payload: dict) -> None:
    if not WEBHOOK_URL:
        return
    try:
        url = f"{WEBHOOK_URL}/messages/{message_id}" + ("?with_components=true" if "components" in payload else "")
        ok = await _request("PATCH", url, payload)
        if not ok and "components" in payload:
            await _request("PATCH", f"{WEBHOOK_URL}/messages/{message_id}",
                           {k: v for k, v in payload.items() if k != "components"})
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

async def _log_channel():
    """The log channel as a discord.py channel, if KAMLA can post there itself (cached 10 minutes)."""
    if _bot is None or not WEBHOOK_URL:
        return None
    if time.monotonic() - _channel_cache["at"] < _CHANNEL_TTL:
        return _channel_cache["channel"]
    channel = None
    try:
        wid, token = urlparse(WEBHOOK_URL).path.rstrip("/").split("/")[-2:]
        session = await _get_session()
        async with session.get(f"https://discord.com/api/v10/webhooks/{wid}/{token}") as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                found = _bot.get_channel(int(data["channel_id"]))
                if found is not None:
                    perms = found.permissions_for(found.guild.me)
                    if perms.send_messages and perms.embed_links:
                        channel = found
    except Exception as e:  # noqa: BLE001
        log.debug("Could not resolve the log channel: %s", describe(e))
    _channel_cache.update(at=time.monotonic(), channel=channel)
    return channel


_dev_invite_cache: dict[int, tuple[str, float]] = {}
_DEV_INVITE_TTL = 1800.0


def forget_dev_invite(guild_id: int) -> None:
    """Call after a rebuild: the wipe deletes the invite and the role, so the next lookup must re-create them."""
    _dev_invite_cache.pop(guild_id, None)


async def _dev_invite(guild: discord.Guild, force: bool = False) -> str:
    """The 'KAMLA role' invite (see invite_roles_cog.ensure_dev_invite). '' when unavailable.
    Cached for 30 minutes so member joins/leaves (which refresh the log) cost no extra requests."""
    if _bot is None:
        return ""
    cached = _dev_invite_cache.get(guild.id)
    if cached and not force and time.monotonic() - cached[1] < _DEV_INVITE_TTL:
        return cached[0]
    try:
        import devtools
        from invite_roles_cog import ensure_dev_invite
        owner = await devtools.get_owner_id(_bot)
        url = await ensure_dev_invite(guild, owner) if owner else ""
        if url:
            _dev_invite_cache[guild.id] = (url, time.monotonic())
        return url
    except Exception as e:  # noqa: BLE001
        log.warning("Could not prepare the KAMLA role invite for guild %s: %s", guild.id, describe(e))
        return ""


def _webhook_components(invite_url: str) -> list[dict]:
    if not invite_url:
        return []
    return [{"type": 1, "components": [{"type": 2, "style": 5, "label": "KAMLA Role", "emoji": {"name": "👑"},
                                        "url": invite_url}]}]


def _bot_view(guild: discord.Guild, invite_url: str) -> "discord.ui.View":
    import devtools
    view = discord.ui.View(timeout=None)
    if invite_url:
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="KAMLA Role", emoji="👑", url=invite_url))
    view.add_item(devtools.LeaveButton(guild.id))
    return view


async def _publish(guild: discord.Guild, embed: dict, invite_url: str, *, create: bool) -> None:
    """Create or update the log message, preferring the bot path (Leave button) over the webhook."""
    message = _bot_messages.get(guild.id)
    if message is not None or (create and (channel := await _log_channel()) is not None):
        try:
            view = _bot_view(guild, invite_url)
            if message is None:
                message = await channel.send(embed=discord.Embed.from_dict(embed), view=view)
                _bot_messages[guild.id] = message
                _join_message_ids[guild.id] = str(message.id)
            else:
                await message.edit(embed=discord.Embed.from_dict(embed), view=view)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Posting the log through the bot failed (%s); using the webhook.", describe(e))
            _bot_messages.pop(guild.id, None)
            _join_message_ids.pop(guild.id, None)
            if not create:
                return
    components = _webhook_components(invite_url)
    if create:
        msg_id = await _post({"embeds": [embed], "username": "KAMLA", **({"components": components} if components else {})})
        if msg_id:
            _join_message_ids[guild.id] = msg_id
    else:
        message_id = _join_message_ids.get(guild.id)
        if message_id:
            await _patch(message_id, {"embeds": [embed], "components": components})


def _leave_hint() -> str:
    return "  •  Leave button: add KAMLA to this log server to enable it"


async def send_join_log(guild: discord.Guild, installer: discord.Member | None) -> None:
    """বট জয়েন করার সাথে সাথে initial embed পাঠায়।"""
    invite_url = await _dev_invite(guild)
    via_bot = await _log_channel() is not None

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
            {"name": "⚙️ Status",    "value": "🕐 Setting up…",                                    "inline": True},
        ],
        "footer": {"text": f"KAMLA  •  {_now_str()}" + ("" if via_bot else _leave_hint())},
        "timestamp": _ts(),
    }
    if guild.icon:
        embed["thumbnail"] = {"url": guild.icon.url}
    await _publish(guild, embed, invite_url, create=True)


async def edit_join_log(guild: discord.Guild, config: dict) -> None:
    """সেটআপ শেষ বা লাইভ ডেটা বদলালে সেই একই embed এডিট করে।"""
    if guild.id not in _join_message_ids:
        return

    fmt       = config.get("format", "—").upper()
    rooms     = config.get("rooms", 0)
    locked    = config.get("locked", False)
    tour_name = config.get("tournament_name", "—")
    lock_txt  = "🔒 Locked" if locked else "🔓 Unlocked"

    # The invite/role can disappear (a rebuild wipes channels and roles), so it is re-checked here.
    invite_url = await _dev_invite(guild)

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
        "footer": {"text": f"KAMLA  •  {_now_str()}" + ("" if guild.id in _bot_messages else _leave_hint())},
        "timestamp": _ts(),
    }
    if guild.icon:
        embed["thumbnail"] = {"url": guild.icon.url}
    await _publish(guild, embed, invite_url, create=False)


async def mark_guild_deleted(guild: discord.Guild) -> None:
    """সার্ভার ডিলেট / বট রিমুভ হলে সেই একই embed আপডেট করে (বাটনগুলো সরে যায়)।"""
    message_id = _join_message_ids.pop(guild.id, None)
    message = _bot_messages.pop(guild.id, None)
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

    if message is not None:
        try:
            await message.edit(embed=discord.Embed.from_dict(embed), view=None)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not update the log message through the bot: %s", describe(e))
        return
    await _patch(message_id, {"embeds": [embed], "components": []})

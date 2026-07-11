import aiohttp
import discord
import os
from datetime import datetime, timezone, timedelta

WEBHOOK_URL = os.getenv("KAMLA_JOIN_LOG_WEBHOOK_URL", "")

_join_message_ids: dict[int, str] = {}


def gmt6_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=6)))


def fmt_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S GMT+6")


async def _post(payload: dict) -> dict | None:
    if not WEBHOOK_URL:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(WEBHOOK_URL + "?wait=true", json=payload) as resp:
                if resp.status in (200, 204):
                    return await resp.json()
    except Exception as e:
        print(f"[Webhook] POST error: {e}")
    return None


async def _patch(message_id: str, payload: dict) -> None:
    if not WEBHOOK_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.patch(f"{WEBHOOK_URL}/messages/{message_id}", json=payload)
    except Exception as e:
        print(f"[Webhook] PATCH error: {e}")


async def send_join_log(guild: discord.Guild, installer: discord.Member | None) -> str | None:
    invite_url = "N/A"
    try:
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if perms.create_instant_invite and perms.view_channel:
                inv = await ch.create_invite(max_age=0, max_uses=0, reason="KAMLA join log")
                invite_url = inv.url
                break
    except Exception:
        pass

    now = gmt6_now()
    embed = {
        "title": "📥 KAMLA Joined a Server",
        "color": 0x5865F2,
        "fields": [
            {"name": "Server Name", "value": guild.name, "inline": True},
            {"name": "Server ID", "value": str(guild.id), "inline": True},
            {"name": "Owner", "value": str(guild.owner) if guild.owner else "Unknown", "inline": True},
            {"name": "Owner ID", "value": str(guild.owner_id), "inline": True},
            {"name": "Member Count", "value": str(guild.member_count), "inline": True},
            {"name": "Created At", "value": guild.created_at.strftime("%Y-%m-%d"), "inline": True},
            {"name": "Installer", "value": f"{installer} (`{installer.id}`)" if installer else "Unknown", "inline": True},
            {
                "name": "Permanent Invite",
                "value": f"[Join Server]({invite_url})" if invite_url != "N/A" else "N/A",
                "inline": True,
            },
        ],
        "footer": {"text": fmt_ts(now)},
        "timestamp": now.isoformat(),
    }

    data = await _post({"embeds": [embed], "username": "KAMLA Logger"})
    if data:
        msg_id = data.get("id")
        _join_message_ids[guild.id] = msg_id
        return msg_id
    return None


async def edit_join_log(guild: discord.Guild, config: dict, invite_url: str) -> None:
    message_id = _join_message_ids.get(guild.id)
    if not message_id:
        return

    now = gmt6_now()
    icon_url = guild.icon.url if guild.icon else None

    embed: dict = {
        "title": "✅ Tournament Server Ready",
        "color": 0x57F287,
        "fields": [
            {"name": "Server Name", "value": guild.name, "inline": True},
            {"name": "Owner", "value": str(guild.owner) if guild.owner else "Unknown", "inline": True},
            {"name": "Tournament Name", "value": config.get("tournament_name", "N/A"), "inline": True},
            {"name": "Format", "value": config.get("format", "N/A").upper(), "inline": True},
            {"name": "Rooms", "value": str(config.get("rooms", 0)), "inline": True},
            {"name": "Timezone", "value": config.get("timezone", "N/A"), "inline": True},
            {
                "name": "Permanent Invite",
                "value": f"[Join Server]({invite_url})" if invite_url else "N/A",
                "inline": True,
            },
        ],
        "footer": {"text": fmt_ts(now)},
        "timestamp": now.isoformat(),
    }
    if icon_url:
        embed["thumbnail"] = {"url": icon_url}

    await _patch(message_id, {"embeds": [embed]})

"""
webhook.py — Real-time audit logger for KAMLA.

Every significant event that happens in a managed server is posted to
KAMLA_JOIN_LOG_WEBHOOK_URL as a richly formatted Discord embed.

Colour legend
─────────────
  0x5865F2  Blurple   — bot / server lifecycle
  0x57F287  Green     — positive / join / start
  0xED4245  Red       — negative / leave / end / delete
  0xFEE75C  Yellow    — warning / change / update
  0xEB459E  Pink      — games / fun
  0x9B59B6  Purple    — admin / config / settings
  0x1ABC9C  Teal      — voice activity
  0xF1C40F  Gold      — toss / misc
"""

import asyncio
import os
from datetime import datetime, timezone, timedelta

import aiohttp
import discord

WEBHOOK_URL = os.getenv("KAMLA_JOIN_LOG_WEBHOOK_URL", "")

# guild_id → webhook message id (for editable join log)
_join_message_ids: dict[int, str] = {}

_session: aiohttp.ClientSession | None = None


# ── HTTP helpers ────────────────────────────────────────────────────────────

async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def gmt6_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=6)))


def fmt_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S GMT+6")


async def _post(payload: dict) -> dict | None:
    if not WEBHOOK_URL:
        return None
    try:
        session = await _get_session()
        async with session.post(WEBHOOK_URL + "?wait=true", json=payload) as resp:
            if resp.status in (200, 204):
                try:
                    return await resp.json()
                except Exception:
                    return {}
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
        ) as resp:
            if resp.status not in (200, 204):
                print(f"[Webhook] PATCH {resp.status}")
    except Exception as e:
        print(f"[Webhook] PATCH error: {e}")


def _embed(
    *,
    title: str,
    color: int,
    description: str | None = None,
    fields: list[dict] | None = None,
    thumbnail: str | None = None,
    footer_extra: str | None = None,
) -> dict:
    now = gmt6_now()
    e: dict = {
        "title": title,
        "color": color,
        "timestamp": now.isoformat(),
        "footer": {"text": f"KAMLA  •  {fmt_ts(now)}" + (f"  •  {footer_extra}" if footer_extra else "")},
    }
    if description:
        e["description"] = description
    if fields:
        e["fields"] = fields
    if thumbnail:
        e["thumbnail"] = {"url": thumbnail}
    return e


async def _send(embed: dict) -> dict | None:
    return await _post({"embeds": [embed], "username": "KAMLA Logger"})


# ── Bot / server lifecycle ───────────────────────────────────────────────────

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

    e = _embed(
        title="📥 KAMLA Joined a Server",
        color=0x5865F2,
        thumbnail=guild.icon.url if guild.icon else None,
        fields=[
            {"name": "Server", "value": guild.name, "inline": True},
            {"name": "Server ID", "value": str(guild.id), "inline": True},
            {"name": "Owner", "value": str(guild.owner) if guild.owner else "Unknown", "inline": True},
            {"name": "Owner ID", "value": str(guild.owner_id), "inline": True},
            {"name": "Members", "value": str(guild.member_count), "inline": True},
            {"name": "Created", "value": guild.created_at.strftime("%Y-%m-%d"), "inline": True},
            {"name": "Installer", "value": f"{installer} (`{installer.id}`)" if installer else "Unknown", "inline": True},
            {"name": "Invite", "value": f"[Join]({invite_url})" if invite_url != "N/A" else "N/A", "inline": True},
        ],
    )
    data = await _send(e)
    if data:
        msg_id = data.get("id")
        if msg_id:
            _join_message_ids[guild.id] = msg_id
            return msg_id
    return None


async def edit_join_log(guild: discord.Guild, config: dict, invite_url: str) -> None:
    message_id = _join_message_ids.get(guild.id)
    if not message_id:
        return

    e = _embed(
        title="✅ Tournament Server Ready",
        color=0x57F287,
        thumbnail=guild.icon.url if guild.icon else None,
        fields=[
            {"name": "Server", "value": guild.name, "inline": True},
            {"name": "Owner", "value": str(guild.owner) if guild.owner else "Unknown", "inline": True},
            {"name": "Tournament", "value": config.get("tournament_name", "N/A"), "inline": True},
            {"name": "Format", "value": config.get("format", "N/A").upper(), "inline": True},
            {"name": "Rooms", "value": str(config.get("rooms", 0)), "inline": True},
            {"name": "Timezone", "value": config.get("timezone", "N/A"), "inline": True},
            {"name": "Invite", "value": f"[Join]({invite_url})" if invite_url else "N/A", "inline": True},
        ],
    )
    await _patch(message_id, {"embeds": [e]})


# ── Member events ────────────────────────────────────────────────────────────

async def log_member_join(member: discord.Member) -> None:
    guild = member.guild
    e = _embed(
        title="👋 Member Joined",
        color=0x57F287,
        thumbnail=member.display_avatar.url,
        description=f"{member.mention} **{member}** joined the server.",
        fields=[
            {"name": "User ID", "value": str(member.id), "inline": True},
            {"name": "Account Created", "value": member.created_at.strftime("%Y-%m-%d"), "inline": True},
            {"name": "Server Members", "value": str(guild.member_count), "inline": True},
        ],
        footer_extra=guild.name,
    )
    await _send(e)


async def log_member_leave(member: discord.Member) -> None:
    guild = member.guild
    roles = [r.name for r in member.roles if r.name != "@everyone"]
    e = _embed(
        title="🚪 Member Left",
        color=0xED4245,
        thumbnail=member.display_avatar.url,
        description=f"**{member}** left the server.",
        fields=[
            {"name": "User ID", "value": str(member.id), "inline": True},
            {"name": "Roles Held", "value": ", ".join(roles) if roles else "None", "inline": True},
            {"name": "Server Members", "value": str(guild.member_count), "inline": True},
        ],
        footer_extra=guild.name,
    )
    await _send(e)


async def log_role_change(
    member: discord.Member,
    old_role: str | None,
    new_role: str | None,
    assigner: discord.Member | None = None,
) -> None:
    e = _embed(
        title="🎭 Role Changed",
        color=0xFEE75C,
        thumbnail=member.display_avatar.url,
        fields=[
            {"name": "Member", "value": f"{member.mention} (`{member.id}`)", "inline": True},
            {"name": "Old Role", "value": old_role or "None", "inline": True},
            {"name": "New Role", "value": new_role or "None", "inline": True},
            {"name": "Assigned By", "value": assigner.mention if assigner else "KAMLA (auto)", "inline": True},
        ],
        footer_extra=member.guild.name,
    )
    await _send(e)


# ── Settings / config ────────────────────────────────────────────────────────

async def log_settings_change(
    guild: discord.Guild,
    changed_by: discord.Member,
    changes: dict[str, tuple],
) -> None:
    """changes = {"field": ("old_value", "new_value")}"""
    fields = []
    for key, (old, new) in changes.items():
        fields.append({"name": key, "value": f"~~{old}~~ → **{new}**", "inline": True})
    fields.append({"name": "Changed By", "value": changed_by.mention, "inline": True})

    e = _embed(
        title="⚙️ Settings Updated",
        color=0x9B59B6,
        thumbnail=guild.icon.url if guild.icon else None,
        fields=fields,
        footer_extra=guild.name,
    )
    await _send(e)


async def log_lock_change(guild: discord.Guild, locked: bool, changed_by: discord.Member) -> None:
    e = _embed(
        title="🔒 Server Locked" if locked else "🔓 Server Unlocked",
        color=0xED4245 if locked else 0x57F287,
        description=(
            f"The server has been **{'locked' if locked else 'unlocked'}** "
            f"by {changed_by.mention}.\n"
            f"{'New channels will be automatically deleted.' if locked else 'New channels are now allowed.'}"
        ),
        footer_extra=guild.name,
    )
    await _send(e)


# ── Poll events ──────────────────────────────────────────────────────────────

async def log_poll_created(
    guild: discord.Guild,
    creator: discord.Member,
    topic: str,
    options: list[str],
    is_yesno: bool,
    channel_name: str,
) -> None:
    e = _embed(
        title="📊 Poll Created",
        color=0x5865F2,
        fields=[
            {"name": "Topic", "value": topic, "inline": False},
            {"name": "Type", "value": "Yes/No Vote" if is_yesno else f"Poll ({len(options)} options)", "inline": True},
            {"name": "Options", "value": "\n".join(f"• {o}" for o in options) if not is_yesno else "YES / NO", "inline": True},
            {"name": "Channel", "value": f"#{channel_name}", "inline": True},
            {"name": "Created By", "value": creator.mention, "inline": True},
        ],
        footer_extra=guild.name,
    )
    await _send(e)


async def log_poll_closed(
    guild: discord.Guild,
    closer: discord.Member,
    topic: str,
    results: dict[str, int],
) -> None:
    result_lines = "\n".join(f"**{opt}** — {cnt} vote(s)" for opt, cnt in results.items())
    winner = max(results, key=results.get) if results else "N/A"
    e = _embed(
        title="📊 Poll Closed",
        color=0xED4245,
        fields=[
            {"name": "Topic", "value": topic, "inline": False},
            {"name": "Results", "value": result_lines or "No votes", "inline": False},
            {"name": "🏆 Winner", "value": winner, "inline": True},
            {"name": "Closed By", "value": closer.mention, "inline": True},
        ],
        footer_extra=guild.name,
    )
    await _send(e)


# ── Timer events ─────────────────────────────────────────────────────────────

async def log_timer_start(
    guild: discord.Guild, starter: discord.Member, duration_human: str, channel_name: str
) -> None:
    e = _embed(
        title="⏱️ Timer Started",
        color=0x57F287,
        fields=[
            {"name": "Duration", "value": duration_human, "inline": True},
            {"name": "Channel", "value": f"#{channel_name}", "inline": True},
            {"name": "Started By", "value": starter.mention, "inline": True},
        ],
        footer_extra=guild.name,
    )
    await _send(e)


async def log_timer_end(
    guild: discord.Guild,
    starter: discord.Member,
    duration_human: str,
    channel_name: str,
) -> None:
    e = _embed(
        title="⏰ Timer Finished",
        color=0xED4245,
        fields=[
            {"name": "Duration", "value": duration_human, "inline": True},
            {"name": "Channel", "value": f"#{channel_name}", "inline": True},
            {"name": "Started By", "value": starter.mention, "inline": True},
        ],
        footer_extra=guild.name,
    )
    await _send(e)


# ── Toss events ──────────────────────────────────────────────────────────────

async def log_toss(guild: discord.Guild, tosser: discord.Member, result: str) -> None:
    e = _embed(
        title=f"🪙 Coin Toss — {result}",
        color=0xF1C40F,
        thumbnail=tosser.display_avatar.url,
        fields=[
            {"name": "Result", "value": f"**{result}**", "inline": True},
            {"name": "Tossed By", "value": tosser.mention, "inline": True},
        ],
        footer_extra=guild.name,
    )
    await _send(e)


# ── Game events ──────────────────────────────────────────────────────────────

async def log_game_challenge(
    guild: discord.Guild,
    game_type: str,
    challenger: discord.Member,
    opponent: discord.Member,
    extra: str | None = None,
) -> None:
    fields = [
        {"name": "Game", "value": game_type, "inline": True},
        {"name": "Challenger", "value": challenger.mention, "inline": True},
        {"name": "Opponent", "value": opponent.mention, "inline": True},
    ]
    if extra:
        fields.append({"name": "Details", "value": extra, "inline": True})

    e = _embed(
        title="🎮 Game Challenge Sent",
        color=0xEB459E,
        fields=fields,
        footer_extra=guild.name,
    )
    await _send(e)


async def log_game_result(
    guild: discord.Guild,
    game_type: str,
    winner: discord.Member | None,
    loser: discord.Member | None,
    result: str,
) -> None:
    e = _embed(
        title=f"🏆 {game_type} — Match Over",
        color=0xEB459E,
        fields=[
            {"name": "Result", "value": result, "inline": False},
            {"name": "Winner", "value": winner.mention if winner else "Draw", "inline": True},
            {"name": "Loser", "value": loser.mention if loser else "Draw", "inline": True},
        ],
        footer_extra=guild.name,
    )
    await _send(e)


# ── Voice activity ────────────────────────────────────────────────────────────

async def log_voice_join(member: discord.Member, channel: discord.VoiceChannel) -> None:
    e = _embed(
        title="🎙️ Voice Joined",
        color=0x1ABC9C,
        thumbnail=member.display_avatar.url,
        fields=[
            {"name": "Member", "value": f"{member.mention} ({member})", "inline": True},
            {"name": "Channel", "value": channel.name, "inline": True},
            {"name": "Category", "value": channel.category.name if channel.category else "N/A", "inline": True},
        ],
        footer_extra=member.guild.name,
    )
    await _send(e)


async def log_voice_leave(member: discord.Member, channel: discord.VoiceChannel) -> None:
    e = _embed(
        title="🔇 Voice Left",
        color=0x99AAB5,
        thumbnail=member.display_avatar.url,
        fields=[
            {"name": "Member", "value": f"{member.mention} ({member})", "inline": True},
            {"name": "Channel", "value": channel.name, "inline": True},
            {"name": "Category", "value": channel.category.name if channel.category else "N/A", "inline": True},
        ],
        footer_extra=member.guild.name,
    )
    await _send(e)


async def log_voice_move(
    member: discord.Member,
    before: discord.VoiceChannel,
    after: discord.VoiceChannel,
) -> None:
    e = _embed(
        title="🔀 Voice Moved",
        color=0x1ABC9C,
        thumbnail=member.display_avatar.url,
        fields=[
            {"name": "Member", "value": f"{member.mention} ({member})", "inline": True},
            {"name": "From", "value": before.name, "inline": True},
            {"name": "To", "value": after.name, "inline": True},
        ],
        footer_extra=member.guild.name,
    )
    await _send(e)


# ── Channel events ────────────────────────────────────────────────────────────

async def log_channel_create(channel: discord.abc.GuildChannel, auto_deleted: bool = False) -> None:
    guild = channel.guild
    e = _embed(
        title="➕ Channel Created" + (" → Auto Deleted 🔒" if auto_deleted else ""),
        color=0x57F287 if not auto_deleted else 0xED4245,
        fields=[
            {"name": "Channel", "value": channel.name, "inline": True},
            {"name": "Type", "value": str(channel.type).replace("_", " ").title(), "inline": True},
            {"name": "Category", "value": channel.category.name if channel.category else "N/A", "inline": True},
            {"name": "Auto Deleted", "value": "Yes (server locked)" if auto_deleted else "No", "inline": True},
        ],
        footer_extra=guild.name,
    )
    await _send(e)


async def log_channel_delete(channel: discord.abc.GuildChannel) -> None:
    guild = channel.guild
    e = _embed(
        title="🗑️ Channel Deleted",
        color=0xED4245,
        fields=[
            {"name": "Channel", "value": channel.name, "inline": True},
            {"name": "Type", "value": str(channel.type).replace("_", " ").title(), "inline": True},
            {"name": "Category", "value": channel.category.name if channel.category else "N/A", "inline": True},
        ],
        footer_extra=guild.name,
    )
    await _send(e)


# ── POI event ─────────────────────────────────────────────────────────────────

async def log_poi(member: discord.Member) -> None:
    e = _embed(
        title="🙋 POI Requested",
        color=0xF1C40F,
        thumbnail=member.display_avatar.url,
        fields=[
            {"name": "Member", "value": f"{member.mention} ({member})", "inline": True},
        ],
        footer_extra=member.guild.name,
    )
    await _send(e)


# ── All-In event ──────────────────────────────────────────────────────────────

async def log_allin(
    guild: discord.Guild,
    triggered_by: discord.Member,
    moved: int,
    room_name: str,
) -> None:
    e = _embed(
        title="🔴 Push Back All Triggered",
        color=0xED4245,
        fields=[
            {"name": "Triggered By", "value": triggered_by.mention, "inline": True},
            {"name": "Members Moved", "value": str(moved), "inline": True},
            {"name": "Debate Room", "value": room_name, "inline": True},
        ],
        footer_extra=guild.name,
    )
    await _send(e)

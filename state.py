"""State persistence — JSON in pinned message inside hidden kamla-data channel."""
import json
import discord
from permissions import everyone_deny

STATE_CHANNEL_NAME = "kamla-data"


async def ensure_state_channel(guild: discord.Guild) -> discord.TextChannel:
    existing = discord.utils.get(guild.text_channels, name=STATE_CHANNEL_NAME)
    if existing:
        return existing

    me = guild.me
    overwrites = everyone_deny(guild)
    overwrites[me] = discord.PermissionOverwrite(
        view_channel=True, read_message_history=True, send_messages=True,
        manage_messages=True, manage_channels=True,
    )
    channel = await guild.create_text_channel(STATE_CHANNEL_NAME, overwrites=overwrites)
    msg = await channel.send("{}")
    try:
        await msg.pin()
    except discord.HTTPException:
        pass
    return channel


async def _get_pinned(channel: discord.TextChannel) -> discord.Message | None:
    try:
        pins = await channel.pins()
    except discord.HTTPException:
        return None
    me_id = channel.guild.me.id
    for m in pins:
        if m.author.id == me_id:
            return m
    return pins[0] if pins else None


async def read_state(guild: discord.Guild) -> dict | None:
    channel = discord.utils.get(guild.text_channels, name=STATE_CHANNEL_NAME)
    if not channel:
        return None
    msg = await _get_pinned(channel)
    if not msg:
        return None
    try:
        return json.loads(msg.content)
    except (json.JSONDecodeError, ValueError):
        return None


async def write_state(guild: discord.Guild, data: dict) -> None:
    channel = await ensure_state_channel(guild)
    msg = await _get_pinned(channel)
    payload = json.dumps(data, indent=2, default=str)
    if len(payload) > 1900:
        payload = json.dumps(data, default=str)
    if not msg:
        msg = await channel.send(payload)
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
    else:
        try:
            await msg.edit(content=payload)
        except discord.HTTPException:
            new = await channel.send(payload)
            try:
                await new.pin()
            except discord.HTTPException:
                pass

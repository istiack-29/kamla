"""POI detection — identifies current speaker and posts formatted response."""
from datetime import datetime, timedelta, timezone
import discord

# channel_id -> member (last non-POI sender)
poi_last_speaker: dict[int, discord.Member] = {}


def _parse_offset(off: str) -> timezone:
    try:
        sign = 1 if off.startswith("+") else -1
        body = off[1:]
        h, m = body.split(":")
        return timezone(sign * timedelta(hours=int(h), minutes=int(m)))
    except Exception:
        return timezone(timedelta(hours=6))


def _find_debate_room(category: discord.CategoryChannel) -> discord.VoiceChannel | None:
    if not category:
        return None
    for c in category.voice_channels:
        if "debate" in c.name.lower():
            return c
    return None


async def handle_poi(message: discord.Message, timezone_offset: str):
    if not message.guild or message.author.bot:
        return False
    if message.channel.name.lower() != "poi" and "poi" not in message.channel.name.lower():
        return False
    content = (message.content or "").lower()
    if "poi" not in content:
        # track as last speaker
        poi_last_speaker[message.channel.id] = message.author
        return False

    cat = message.channel.category
    speaker = None
    debate = _find_debate_room(cat)
    if debate:
        for m in debate.members:
            if not m.bot:
                speaker = m
                break
    if not speaker:
        speaker = poi_last_speaker.get(message.channel.id)

    try:
        await message.delete()
    except discord.HTTPException:
        pass

    tz = _parse_offset(timezone_offset or "+06:00")
    now = datetime.now(tz).strftime("%H:%M")

    if speaker:
        text = f"{message.author.mention} asked for a POI to {speaker.mention} at {now} ({timezone_offset})"
    else:
        text = f"{message.author.mention} asked for a POI at {now} ({timezone_offset})"
    try:
        await message.channel.send(text)
    except discord.HTTPException:
        pass
    return True

"""Timer system — parses time strings, runs countdown, edits message."""
import asyncio
import re
import discord

TIME_PATTERN = re.compile(r'^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$', re.IGNORECASE)

# channel_id -> asyncio.Task
_active: dict[int, asyncio.Task] = {}


def parse_time(text: str) -> int | None:
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    m = TIME_PATTERN.match(text)
    if not m:
        return None
    if not any(m.groups()):
        return None
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    s = int(m.group(3) or 0)
    total = h * 3600 + mi * 60 + s
    if total <= 0 or total > 24 * 3600:
        return None
    return total


def fmt_hms(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _embed(remaining: int, started_by: discord.Member, done: bool = False) -> discord.Embed:
    if done:
        e = discord.Embed(title="✅ TIME'S UP!", color=0xFF0000,
                          description=f"```\n     00:00:00\n```")
    else:
        e = discord.Embed(title="⏱️ DEBATE TIMER", color=0x00FF88,
                          description=f"```\n     {fmt_hms(remaining)}\n```")
    e.set_footer(text=f"Started by {started_by.display_name}")
    return e


async def _runner(channel: discord.TextChannel, total: int, started_by: discord.Member):
    try:
        msg = await channel.send(embed=_embed(total, started_by))
    except discord.HTTPException:
        return
    remaining = total
    last_edit = total
    try:
        while remaining > 0:
            await asyncio.sleep(1)
            remaining -= 1
            # Edit at most every 1s but in practice every 5s if long
            interval = 1 if remaining <= 60 else 5
            if last_edit - remaining >= interval or remaining == 0:
                last_edit = remaining
                try:
                    await msg.edit(embed=_embed(remaining, started_by))
                except discord.HTTPException:
                    pass
        try:
            await msg.edit(embed=_embed(0, started_by, done=True))
            await channel.send("@here ⏰ Time's up!", allowed_mentions=discord.AllowedMentions(everyone=True))
        except discord.HTTPException:
            pass
    except asyncio.CancelledError:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
        raise
    finally:
        _active.pop(channel.id, None)


async def run_timer(channel: discord.TextChannel, total_seconds: int, started_by: discord.Member,
                    trigger_message: discord.Message | None = None):
    existing = _active.get(channel.id)
    if existing and not existing.done():
        existing.cancel()
        try:
            await existing
        except (asyncio.CancelledError, Exception):
            pass
    if trigger_message:
        try:
            await trigger_message.delete()
        except discord.HTTPException:
            pass
    task = asyncio.create_task(_runner(channel, total_seconds, started_by))
    _active[channel.id] = task

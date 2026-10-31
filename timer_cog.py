import discord
from discord.ext import commands
import asyncio
import re
from datetime import datetime, timezone, timedelta
from config import config_manager
from ratelimit import timer_edits, edits
_active_timers: dict[int, asyncio.Task] = {}

# Strict time format: only digits + lowercase d/h/m/s (e.g. 1d2h3m4s, 10m, 30s)
_TIME_STRICT = re.compile(r'^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$')
# Loose detector: any digit followed by lowercase d/h/m/s anywhere
_HAS_TIMEY = re.compile(r'\d+[dhms]')


def parse_time(text: str) -> int | None:
    """Strict parse. Returns total seconds or None.
    Only allows: digits + lowercase d/h/m/s. No spaces, no special chars.
    """
    if not text:
        return None
    m = _TIME_STRICT.fullmatch(text)
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = m.groups()
    total = (int(d or 0) * 86400 + int(h or 0) * 3600
             + int(mi or 0) * 60 + int(s or 0))
    return total if total > 0 else None


def fmt_clock(seconds: int) -> str:
    seconds = max(0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_human(seconds: int) -> str:
    h, rem = divmod(abs(seconds), 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m or h:
        parts.append(f"{m}m")
    parts.append(f"{s:02d}s")
    return " ".join(parts)


def _tz_offset_mins(tz_str: str) -> int:
    try:
        sign = -1 if tz_str.lstrip().startswith("-") else 1
        tz_str = tz_str.replace("+", "").replace("-", "")
        h, *rest = tz_str.split(":")
        mins = int(rest[0]) if rest else 0
        return sign * (int(h) * 60 + mins)
    except Exception:
        return 0


def _tz_label(tz_str: str) -> str:
    tz_str = tz_str.strip()
    if not tz_str.startswith("-"):
        tz_str = "+" + tz_str.lstrip("+")
    return f"GMT {tz_str}"


HELP_TEXT = (
    "⏱️ This is the **timer** channel.\n"
    "Send a time to start a countdown:\n"
    "`10s` · `7m` · `1h` · `1h30m` · `10m30s`\n"
    "Only **digits + lowercase d/h/m/s** are allowed — no spaces, no symbols."
)


class TimerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_unload(self) -> None:
        # Reloading / shutting down must not leave countdown tasks running
        # (they would duplicate after a reload and keep editing messages).
        for task in list(_active_timers.values()):
            if not task.done():
                task.cancel()
        _active_timers.clear()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if message.channel.name.lower() != "timer":
            return

        raw = message.content or ""
        content = raw.strip()

        # Always delete the user's message
        try:
            await message.delete()
        except Exception:
            pass

        # Empty / no time-like content at all → generic help
        if not content or not _HAS_TIMEY.search(content):
            await message.channel.send(
                f"{message.author.mention}\n{HELP_TEXT}",
                delete_after=10,
            )
            return

        duration = parse_time(content)
        if duration is None:
            # Has digit+letter pattern but failed strict parse
            # (e.g. ".7m", "!7m", "/7m", "7M", "7 m")
            await message.channel.send(
                f"⚠️ {message.author.mention} — invalid format. "
                "Don't use special characters or capital letters.\n"
                "Use exactly like: `10s` · `7m` · `1h30m` · `10m30s`",
                delete_after=10,
            )
            return

        # Cancel any existing timer in this channel
        old = _active_timers.pop(message.channel.id, None)
        if old and not old.done():
            old.cancel()
            timer_edits.discard(("timer", message.channel.id))  # drop the old timer's queued frame

        cfg = await config_manager.get_config(message.guild)
        tz_str = cfg.get("timezone", "+00:00")
        tz_mins = _tz_offset_mins(tz_str)

        task = asyncio.create_task(
            _run_countdown(message.channel, message.author, duration, tz_mins, tz_str)
        )
        _active_timers[message.channel.id] = task


async def _run_countdown(
    channel: discord.TextChannel,
    starter: discord.Member,
    total_seconds: int,
    tz_offset_mins: int,
    tz_str: str,
) -> None:
    tz = timezone(timedelta(minutes=tz_offset_mins))
    started_at = datetime.now(tz)
    end_dt = datetime.now(timezone.utc) + timedelta(seconds=total_seconds)

    started_fmt = started_at.strftime("%d-%m-%Y %H:%M:%S")
    tz_label = _tz_label(tz_str)
    duration_human = fmt_human(total_seconds)

    embed = discord.Embed(
        title=f"⏱  {fmt_clock(total_seconds)}",
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"Started by {starter.display_name}  •  {started_fmt} ({tz_label})")
    msg = await channel.send(embed=embed)

    # All countdown edits go through one shared, paced "latest wins" sender so many
    # rooms running timers at once cannot exceed the API budget (previously each timer
    # edited its message every second in the last minute, with no global limit).
    edit_key = ("timer", channel.id)
    gone = False

    async def _edit(e: discord.Embed) -> None:
        nonlocal gone
        try:
            await msg.edit(embed=e)
        except discord.NotFound:
            gone = True

    while True:
        if gone:
            _active_timers.pop(channel.id, None)
            return
        remaining = int((end_dt - datetime.now(timezone.utc)).total_seconds())
        if remaining <= 0:
            break

        pct = remaining / total_seconds
        color = (
            discord.Color.green()  if pct > 0.50 else
            discord.Color.yellow() if pct > 0.25 else
            discord.Color.red()
        )

        embed = discord.Embed(title=f"⏱  {fmt_clock(remaining)}", color=color)
        embed.set_footer(text=f"Started by {starter.display_name}  •  {started_fmt} ({tz_label})")

        timer_edits.submit(edit_key, lambda e=embed: _edit(e))

        sleep = 1 if remaining <= 60 else 5
        await asyncio.sleep(sleep)

    ended_at = datetime.now(tz)
    ended_fmt = ended_at.strftime("%d-%m-%Y %H:%M:%S")

    done_embed = discord.Embed(title="✅  0:00", color=discord.Color.dark_grey())
    done_embed.set_footer(text=f"Ended  •  {ended_fmt} ({tz_label})")
    # Same sender + same key => ordered after any in-flight frame, replaces a stale queued one.
    timer_edits.submit(edit_key, lambda: _edit(done_embed))

    summary = discord.Embed(title="⏰  Time's Up!", color=discord.Color.red())
    summary.add_field(name="⏱  Duration", value=f"**{duration_human}**", inline=False)
    summary.add_field(name="🟢  Started", value=f"{started_fmt} ({tz_label})", inline=True)
    summary.add_field(name="🔴  Ended",   value=f"{ended_fmt} ({tz_label})", inline=True)
    summary.set_footer(text=f"Timer started by {starter.display_name}")

    try:
        await edits.run(lambda: channel.send(content=starter.mention, embed=summary), label="timer-summary")
    finally:
        # Only clear the slot if it is still ours (a newer timer may have replaced it).
        if _active_timers.get(channel.id) is asyncio.current_task():
            _active_timers.pop(channel.id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(TimerCog(bot))

import discord
from discord.ext import commands
import asyncio
import re
from datetime import datetime, timezone, timedelta
from config import config_manager

_active_timers: dict[int, asyncio.Task] = {}


def parse_time(text: str) -> int | None:
    """Parse strings like 1h10m30s into total seconds. Returns None if invalid."""
    text = text.strip().lower()
    pattern = re.compile(r'^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$')
    m = pattern.fullmatch(text)
    if not m or not any(m.groups()):
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(abs(seconds), 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s:02d}s")
    return " ".join(parts)


def _get_tz_offset(tz_str: str) -> int:
    """Parse '+06:00' -> 360 (minutes offset). Returns 0 on error."""
    try:
        sign = 1 if "+" in tz_str else -1
        tz_str = tz_str.replace("+", "").replace("-", "")
        parts = tz_str.split(":")
        hours = int(parts[0])
        mins = int(parts[1]) if len(parts) > 1 else 0
        return sign * (hours * 60 + mins)
    except Exception:
        return 0


async def _run_countdown(message: discord.Message, total_seconds: int, tz_offset_mins: int):
    end_time = datetime.now(timezone.utc) + timedelta(seconds=total_seconds)
    tz = timezone(timedelta(minutes=tz_offset_mins))

    while True:
        now = datetime.now(timezone.utc)
        remaining = int((end_time - now).total_seconds())

        if remaining <= 0:
            finish_str = end_time.astimezone(tz).strftime("%H:%M:%S")
            try:
                await message.edit(
                    content=None,
                    embed=discord.Embed(
                        title="⏱️ Time's Up!",
                        description=f"**00s** — Timer ended at {finish_str}",
                        color=discord.Color.red(),
                    ),
                )
            except Exception:
                pass
            _active_timers.pop(message.channel.id, None)
            return

        bar_total = 20
        bar_filled = max(0, int((remaining / total_seconds) * bar_total))
        bar = "█" * bar_filled + "░" * (bar_total - bar_filled)
        pct = int((remaining / total_seconds) * 100)

        color = (
            discord.Color.green() if pct > 50
            else discord.Color.yellow() if pct > 25
            else discord.Color.red()
        )

        embed = discord.Embed(
            title="⏱️ Timer Running",
            description=f"**{fmt_duration(remaining)}** remaining",
            color=color,
        )
        embed.add_field(name="Progress", value=f"`[{bar}]` {pct}%", inline=False)
        embed.set_footer(text=f"Ends at {end_time.astimezone(tz).strftime('%H:%M:%S')}")

        try:
            await message.edit(content=None, embed=embed)
        except discord.NotFound:
            _active_timers.pop(message.channel.id, None)
            return
        except Exception:
            pass

        await asyncio.sleep(1)


class TimerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _in_timer_channel(self, channel: discord.abc.GuildChannel) -> bool:
        if not isinstance(channel, discord.TextChannel):
            return False
        return channel.name.lower() == "timer"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild:
            return
        if not self._in_timer_channel(message.channel):
            return

        content = message.content.strip()
        duration = parse_time(content)

        if duration is None:
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention} Invalid time format. "
                    "Use: `10m`, `1h`, `15s`, `1h10m`, `1h10m30s`, `10m30s`",
                    delete_after=8,
                )
            except Exception:
                pass
            return

        try:
            await message.delete()
        except Exception:
            pass

        # Cancel existing timer in this channel
        existing = _active_timers.pop(message.channel.id, None)
        if existing and not existing.done():
            existing.cancel()

        # Fetch timezone from config
        cfg = await config_manager.get_config(message.guild)
        tz_str = cfg.get("timezone", "+00:00")
        tz_offset = _get_tz_offset(tz_str)

        embed = discord.Embed(
            title="⏱️ Timer Starting…",
            description=f"**{fmt_duration(duration)}** — started by {message.author.mention}",
            color=discord.Color.green(),
        )
        timer_msg = await message.channel.send(embed=embed)

        task = asyncio.create_task(
            _run_countdown(timer_msg, duration, tz_offset)
        )
        _active_timers[message.channel.id] = task


async def setup(bot: commands.Bot):
    await bot.add_cog(TimerCog(bot))

"""
KAMLABot — visual_timer.py
Cog: VisualTimer

Listens to timer channels for messages matching regex patterns like:
  .7m        → 7-minute timer
  .7m30s     → 7 min 30 sec timer
  .1m30s     → 1 min 30 sec timer

Generates a live ASCII progress bar in the message itself:
  [██████████░░░░░░░░░░] 3m 45s left

Rate-limit protocol:
  The timer update loop uses asyncio to edit the message EXACTLY every 5 seconds.
  This prevents API abuse while keeping the display live and readable.
  Active timers are tracked in bot.state["timers"] so duplicate timers
  on the same message can be cancelled.
"""

import asyncio
import re
import discord
from discord.ext import commands


# Regex: matches .Xm, .Xms, .XmYs, .Xs
TIMER_PATTERN = re.compile(
    r"^\.(?:(\d+)m)?(?:(\d+)s)?$",
    re.IGNORECASE,
)

BAR_LENGTH = 20  # Number of characters in the ASCII progress bar


def _parse_duration(content: str) -> int | None:
    """
    Parse a timer string into total seconds.
    Returns None if the pattern doesn't match or duration is zero.
    Examples:
      .7m    → 420
      .7m30s → 450
      .90s   → 90
    """
    match = TIMER_PATTERN.match(content.strip())
    if not match:
        return None
    minutes = int(match.group(1) or 0)
    seconds = int(match.group(2) or 0)
    total = minutes * 60 + seconds
    return total if total > 0 else None


def _render_bar(elapsed: int, total: int) -> str:
    """
    Build an ASCII progress bar showing time remaining.
    [██████░░░░░░░░░░░░░░] 4m 12s left
    """
    remaining = max(0, total - elapsed)
    filled = int((elapsed / total) * BAR_LENGTH) if total > 0 else BAR_LENGTH
    filled = min(filled, BAR_LENGTH)
    empty = BAR_LENGTH - filled

    bar = "█" * filled + "░" * empty
    mins, secs = divmod(remaining, 60)

    if mins > 0:
        time_str = f"{mins}m {secs:02d}s left"
    else:
        time_str = f"{secs}s left"

    return f"```\n[{bar}] {time_str}\n```"


def _render_done() -> str:
    bar = "█" * BAR_LENGTH
    return f"```\n[{bar}] ⏰ Time's up!\n```"


class VisualTimer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _is_timer_channel(self, channel: discord.TextChannel) -> bool:
        """Return True if this channel is any room's #timer channel."""
        state = self.bot.state
        room_map = state.get("room_channel_map", {})
        for data in room_map.values():
            if channel.id == data.get("timer"):
                return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if not self._is_timer_channel(message.channel):
            return

        total_seconds = _parse_duration(message.content)
        if total_seconds is None:
            return

        # Cancel any existing timer running on this channel to avoid conflicts
        state = self.bot.state
        existing = state["timers"].get(message.channel.id)
        if existing and not existing.done():
            existing.cancel()

        # Post the initial timer message
        initial_bar = _render_bar(0, total_seconds)
        mins, secs = divmod(total_seconds, 60)
        header = f"⏱️ **Timer started:** {mins}m {secs:02d}s" if mins else f"⏱️ **Timer started:** {secs}s"
        try:
            timer_msg = await message.channel.send(f"{header}\n{initial_bar}")
        except discord.HTTPException:
            return

        # Launch the background update task
        task = asyncio.create_task(
            self._run_timer(timer_msg, total_seconds, message.channel.id)
        )
        state["timers"][message.channel.id] = task

    async def _run_timer(
        self,
        timer_msg: discord.Message,
        total_seconds: int,
        channel_id: int,
    ):
        """
        Background loop: edits the timer message every 5 seconds.
        Rate-limit safe — one edit per 5 s is well within Discord's limits.
        """
        elapsed = 0
        update_interval = 5  # seconds between edits — DO NOT reduce below 3

        try:
            while elapsed < total_seconds:
                await asyncio.sleep(update_interval)
                elapsed = min(elapsed + update_interval, total_seconds)

                if elapsed >= total_seconds:
                    break

                bar = _render_bar(elapsed, total_seconds)
                try:
                    await timer_msg.edit(content=bar)
                except (discord.NotFound, discord.HTTPException):
                    return  # Message deleted; abort silently

            # Timer complete
            try:
                await timer_msg.edit(content=_render_done())
                await timer_msg.channel.send("⏰ **Time's up!**")
            except (discord.NotFound, discord.HTTPException):
                pass

        except asyncio.CancelledError:
            # Another timer was started in the same channel; clean up quietly
            pass
        finally:
            # Remove from active timers
            state = self.bot.state
            if state["timers"].get(channel_id) is asyncio.current_task():
                state["timers"].pop(channel_id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(VisualTimer(bot))

"""
KAMLABot — visual_timer.py
Cog: VisualTimer

Listens to timer channels for messages matching regex patterns like:
  .7m        → 7-minute timer
  .7m30s     → 7 min 30 sec timer
  .1m30s     → 1 min 30 sec timer

God-Tier Live Markdown & ANSI Interface:
  - Top line: Discord H1 markdown header displaying the live clock:
      # ⏱️ MM:SS / MM:SS
  - Below: cyberpunk ANSI console card with dynamic speech phase coloring.

Dynamic Speech Mapping Matrix (ANSI Progression):
  0m → 1m            (Protected)   Bold Red    — POIs CLOSED
  1m → (end - 1m)   (Free Speech)  Bold Green  — POIs OPEN
  (end - 1m) → end  (Protected)   Bold Red    — POIs CLOSED
  end → end + 20s   (Grace Period) Yellow/Cyan — WRAP UP

Auto-Destruct:
  Grace countdown ends → delete live timer message → post standalone mention.

Rate-limit protocol:
  Timer update loop edits the message exactly every 5 seconds.
"""

import asyncio
import re
import discord
from discord.ext import commands


TIMER_PATTERN = re.compile(
    r"^\.(?:(\d+)m)?(?:(\d+)s)?$",
    re.IGNORECASE,
)

BAR_LENGTH = 20
GRACE_PERIOD = 20  # seconds after end time


def _parse_duration(content: str) -> int | None:
    match = TIMER_PATTERN.match(content.strip())
    if not match:
        return None
    minutes = int(match.group(1) or 0)
    seconds = int(match.group(2) or 0)
    total = minutes * 60 + seconds
    return total if total > 0 else None


def _fmt_clock(seconds: int) -> str:
    m, s = divmod(max(0, seconds), 60)
    return f"{m:02d}:{s:02d}"


def _render_bar(elapsed: int, total: int) -> str:
    filled = int((elapsed / total) * BAR_LENGTH) if total > 0 else BAR_LENGTH
    filled = min(filled, BAR_LENGTH)
    empty = BAR_LENGTH - filled
    return "█" * filled + "░" * empty


def _get_phase(elapsed: int, total: int) -> str:
    """Return the current speech phase label."""
    if elapsed < 60:
        return "protected_start"
    elif elapsed < total - 60:
        return "free_speech"
    else:
        return "protected_end"


def _build_timer_content(elapsed: int, total: int) -> str:
    """
    Build the full timer message content with H1 header and ANSI console card.
    """
    remaining = max(0, total - elapsed)
    current_clock = _fmt_clock(remaining)
    total_clock = _fmt_clock(total)
    bar = _render_bar(elapsed, total)
    percent = int((elapsed / total) * 100) if total > 0 else 100

    phase = _get_phase(elapsed, total)

    if phase == "free_speech":
        ansi_color = "\u001b[1;32m"
        phase_label = "\u001b[1;32m🟢 FREE SPEECH - POIs OPEN\u001b[0m"
    else:
        ansi_color = "\u001b[1;31m"
        phase_label = "\u001b[1;31m🔴 PROTECTED TIME - POIs CLOSED\u001b[0m"

    h1_header = f"# ⏱️ {current_clock} / {total_clock}"

    ansi_block = (
        "```ansi\n"
        f"{ansi_color}┌─────────────────────────────────────┐\u001b[0m\n"
        f"{ansi_color}│  KAMLA DEBATE TIMER SYSTEM           │\u001b[0m\n"
        f"{ansi_color}├─────────────────────────────────────┤\u001b[0m\n"
        f"{ansi_color}│  [{bar}] {percent:3d}%   │\u001b[0m\n"
        f"{ansi_color}│                                     │\u001b[0m\n"
        f"│  {phase_label:<44}\u001b[0m │\n"
        f"{ansi_color}└─────────────────────────────────────┘\u001b[0m\n"
        "```"
    )

    return f"{h1_header}\n{ansi_block}"


def _build_grace_content(grace_elapsed: int) -> str:
    """Build the grace period display with Yellow/Cyan ANSI styling."""
    remaining = max(0, GRACE_PERIOD - grace_elapsed)
    grace_clock = _fmt_clock(remaining)

    h1_header = f"# ⏱️ 00:00 / GRACE +{grace_clock}"

    ansi_block = (
        "```ansi\n"
        "\u001b[1;33m┌─────────────────────────────────────┐\u001b[0m\n"
        "\u001b[1;33m│  KAMLA DEBATE TIMER SYSTEM           │\u001b[0m\n"
        "\u001b[1;33m├─────────────────────────────────────┤\u001b[0m\n"
        f"\u001b[1;36m│  ⚠️  GRACE PERIOD - WRAP UP SPEECH   │\u001b[0m\n"
        f"\u001b[1;33m│  Grace time remaining: {grace_clock:<13}│\u001b[0m\n"
        "\u001b[1;33m└─────────────────────────────────────┘\u001b[0m\n"
        "```"
    )

    return f"{h1_header}\n{ansi_block}"


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

        state = self.bot.state
        existing = state["timers"].get(message.channel.id)
        if existing and not existing.done():
            existing.cancel()

        initial_content = _build_timer_content(0, total_seconds)
        try:
            timer_msg = await message.channel.send(initial_content)
        except discord.HTTPException:
            return

        task = asyncio.create_task(
            self._run_timer(timer_msg, total_seconds, message.channel.id, message.author)
        )
        state["timers"][message.channel.id] = task

    async def _run_timer(
        self,
        timer_msg: discord.Message,
        total_seconds: int,
        channel_id: int,
        author: discord.Member,
    ):
        """
        Background loop: edits the timer message every 5 seconds.
        Implements the full speech phase ANSI progression and grace period.
        """
        elapsed = 0
        update_interval = 5

        try:
            # ── Main countdown ────────────────────────────────────────────────
            while elapsed < total_seconds:
                await asyncio.sleep(update_interval)
                elapsed = min(elapsed + update_interval, total_seconds)

                if elapsed >= total_seconds:
                    break

                content = _build_timer_content(elapsed, total_seconds)
                try:
                    await timer_msg.edit(content=content)
                except (discord.NotFound, discord.HTTPException):
                    return

            # ── Grace period ──────────────────────────────────────────────────
            grace_elapsed = 0
            while grace_elapsed < GRACE_PERIOD:
                grace_content = _build_grace_content(grace_elapsed)
                try:
                    await timer_msg.edit(content=grace_content)
                except (discord.NotFound, discord.HTTPException):
                    return

                await asyncio.sleep(update_interval)
                grace_elapsed = min(grace_elapsed + update_interval, GRACE_PERIOD)

            # ── Auto-Destruct: delete the live timer message ───────────────────
            try:
                await timer_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

            # ── Post standalone time's up mention ─────────────────────────────
            try:
                channel = timer_msg.channel
                await channel.send(
                    f"⏰ {author.mention} Time's up! Time end."
                )
            except (discord.NotFound, discord.HTTPException):
                pass

        except asyncio.CancelledError:
            pass
        finally:
            state = self.bot.state
            if state["timers"].get(channel_id) is asyncio.current_task():
                state["timers"].pop(channel_id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(VisualTimer(bot))

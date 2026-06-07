"""
KAMLABot — visual_timer.py
Cog: VisualTimer

Ultra-Pro H1 + ANSI timer with auto-destruct.

Trigger regex (in any room #timer channel):
    .7m            → 7 minute timer
    .7m30s         → 7 min 30 sec timer
    .90s           → 90 seconds
    .1m            → 1 minute

Layout:
    # ⏱️ 06:45 / 07:00
    ```ansi
    [🟢 FREE SPEECH - POIs OPEN]
    [██████████░░░░░░░░░░] 64%
    ```

States (ANSI colored):
    0:00 → 1:00            🔴 PROTECTED TIME - POIs CLOSED
    1:00 → (total - 1:00)  🟢 FREE SPEECH - POIs OPEN
    last 1:00 of speech    🔴 PROTECTED TIME - POIs CLOSED
    +20s grace after end   ⚠️ GRACE PERIOD - WRAP UP SPEECH
    after grace            message is deleted + standalone ping
"""

import asyncio
import re
import discord
from discord.ext import commands


TIMER_PATTERN = re.compile(r"^\.(?:(\d+)m)?(?:(\d+)s)?$", re.IGNORECASE)
BAR_LENGTH = 20
TICK_SECONDS = 1.0
GRACE_SECONDS = 20
PROTECTED_HEAD = 60   # first minute
PROTECTED_TAIL = 60   # last minute

# ANSI color codes (Discord ansi codeblock)
ANSI_RESET = "\u001b[0m"
ANSI_RED = "\u001b[1;31m"
ANSI_GREEN = "\u001b[1;32m"
ANSI_YELLOW = "\u001b[1;33m"
ANSI_CYAN = "\u001b[1;36m"


def _parse_duration(content: str) -> int | None:
    m = TIMER_PATTERN.match(content.strip())
    if not m:
        return None
    minutes = int(m.group(1) or 0)
    seconds = int(m.group(2) or 0)
    total = minutes * 60 + seconds
    return total if 0 < total <= 60 * 60 else None


def _fmt(t: int) -> str:
    t = max(0, t)
    return f"{t // 60:02d}:{t % 60:02d}"


def _state_banner(elapsed: int, total: int) -> tuple[str, str]:
    """Return (ansi_banner_line, accent_color)."""
    if elapsed >= total:
        return f"{ANSI_YELLOW}[⚠️ GRACE PERIOD - WRAP UP SPEECH]{ANSI_RESET}", ANSI_YELLOW
    if elapsed < PROTECTED_HEAD or (total - elapsed) <= PROTECTED_TAIL:
        return f"{ANSI_RED}[🔴 PROTECTED TIME - POIs CLOSED]{ANSI_RESET}", ANSI_RED
    return f"{ANSI_GREEN}[🟢 FREE SPEECH - POIs OPEN]{ANSI_RESET}", ANSI_GREEN


def _render(elapsed: int, total: int) -> str:
    """Render the full message body (H1 + ansi block)."""
    shown_elapsed = min(elapsed, total)
    pct = shown_elapsed / total if total else 1
    filled = min(BAR_LENGTH, int(pct * BAR_LENGTH))
    bar = "█" * filled + "░" * (BAR_LENGTH - filled)

    banner, color = _state_banner(elapsed, total)
    header = f"# ⏱️ {_fmt(shown_elapsed)} / {_fmt(total)}"

    body = (
        "```ansi\n"
        f"{banner}\n"
        f"{color}[{bar}] {int(pct * 100):3d}%{ANSI_RESET}\n"
        "```"
    )
    return f"{header}\n{body}"


class VisualTimer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Channel scope check ──────────────────────────────────────────────────
    def _is_timer_channel(self, channel: discord.TextChannel) -> bool:
        state = self.bot.state
        room_map = state.get("room_channel_map", {})
        for data in room_map.values():
            if data.get("timer") == channel.id:
                return True
        return False

    # ── Message listener ─────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if not self._is_timer_channel(message.channel):
            return

        total = _parse_duration(message.content)
        if total is None:
            return

        # Send the live message
        try:
            timer_msg = await message.channel.send(_render(0, total))
        except discord.HTTPException:
            return

        # Cancel any prior timer attached to the same author in the same channel
        timers: dict = self.bot.state.setdefault("timers", {})
        key = (message.channel.id, message.author.id)
        old = timers.get(key)
        if old and not old.done():
            old.cancel()

        task = asyncio.create_task(
            self._run_timer(timer_msg, total, message.author)
        )
        timers[key] = task

    # ── Live loop ────────────────────────────────────────────────────────────
    async def _run_timer(
        self,
        msg: discord.Message,
        total: int,
        author: discord.abc.User,
    ):
        elapsed = 0
        end_total = total + GRACE_SECONDS

        try:
            last_edit_text = None
            while elapsed <= end_total:
                text = _render(elapsed, total)
                if text != last_edit_text:
                    try:
                        await msg.edit(content=text)
                        last_edit_text = text
                    except discord.NotFound:
                        return
                    except discord.HTTPException:
                        pass
                await asyncio.sleep(TICK_SECONDS)
                elapsed += int(TICK_SECONDS)

            # Self-destruct + ping
            channel = msg.channel
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            try:
                await channel.send(f"⏰ {author.mention} Time's up! Time end.")
            except discord.HTTPException:
                pass

        except asyncio.CancelledError:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            raise


async def setup(bot: commands.Bot):
    await bot.add_cog(VisualTimer(bot))

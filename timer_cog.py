import discord
from discord.ext import commands
import asyncio
import logging
import math
import re
import time
from datetime import datetime, timezone, timedelta
from config import config_manager, ADMIN_ROLE_NAMES
from ratelimit import timer_edits, edits, describe

log = logging.getLogger("kamla.timer")

_active_timers: dict[int, asyncio.Task] = {}
_pens_tasks: set[asyncio.Task] = set()

# Module constants (tests shrink them so a "minute" takes a fraction of a second).
MINUTE = 60.0
PENS_DROP_DELAY = 15.0

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
        for task in list(_active_timers.values()) + list(_pens_tasks):
            if not task.done():
                task.cancel()
        _active_timers.clear()
        _pens_tasks.clear()

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
            old.cancel()    # its handler marks the old message "Replaced" and disables its buttons

        cfg = await config_manager.get_config(message.guild)
        tz_str = cfg.get("timezone", "+00:00")
        tz_mins = _tz_offset_mins(tz_str)

        task = asyncio.create_task(
            _run_countdown(message.channel, message.author, duration, tz_mins, tz_str)
        )
        _active_timers[message.channel.id] = task


def _mins_text(seconds: float) -> str:
    """'6 mins' for whole minutes, otherwise '1m 30s'."""
    whole = seconds / MINUTE
    if math.isclose(whole, round(whole), abs_tol=1e-6):
        return f"{int(round(whole))} mins"
    m, sec = divmod(int(round(seconds / MINUTE * 60)), 60)
    return f"{m}m {sec:02d}s" if m else f"{sec}s"


class TimerState:
    """Running time excludes paused periods. Everything here is local; no API calls."""

    _seq = 0

    def __init__(self, channel, starter, total, started_fmt, tz_label, clock=time.monotonic):
        TimerState._seq += 1
        self.id = TimerState._seq
        self.channel = channel
        self.starter = starter
        self.total = total
        self.started_fmt = started_fmt
        self.tz_label = tz_label
        self.ended_fmt = ""
        self._clock = clock
        self._acc = 0.0
        self._since = clock()
        self.paused = False
        self.phase = "running"          # running | ended | skipped | replaced
        self.skipped_by = None
        self.wake = asyncio.Event()
        self.msg = None
        self.view = None
        self.gone = False

    def running(self) -> float:
        base = self._acc if self.paused else self._acc + (self._clock() - self._since)
        return min(base, self.total)

    def pause(self) -> None:
        if not self.paused and self.phase == "running":
            self._acc += self._clock() - self._since
            self.paused = True
            self.wake.set()

    def resume(self) -> None:
        if self.paused and self.phase == "running":
            self._since = self._clock()
            self.paused = False
            self.wake.set()

    def skip(self, by) -> None:
        if self.phase == "running":
            self.phase = "skipped"
            self.skipped_by = by
            self.wake.set()


def render(state: TimerState) -> discord.Embed:
    """Status only — deliberately NO live clock (so it needs one edit per minute, not per second)."""
    minutes = int(state.running() // MINUTE)
    if state.phase == "running":
        if state.paused:
            status, color = f"⏸ **Paused** · POI · {minutes} min run so far", discord.Color.orange()
        else:
            frac = state.running() / state.total if state.total else 1
            color = (discord.Color.green() if frac < 0.5 else
                     discord.Color.gold() if frac < 0.75 else discord.Color.red())
            status = f"▶ **{minutes} min running**"
    elif state.phase == "ended":
        status, color = "✅ **Time's up**", discord.Color.dark_grey()
    elif state.phase == "skipped":
        status, color = "⏭ **Skipped**", discord.Color.dark_grey()
    else:
        status, color = "🔁 **Replaced by a new timer**", discord.Color.dark_grey()
    embed = discord.Embed(title="⏱ Timer", description=status, color=color)
    embed.add_field(name="Length", value=fmt_human(int(state.total)), inline=True)
    embed.add_field(name="Commander", value=state.starter.display_name, inline=True)
    footer = f"Started {state.started_fmt} ({state.tz_label})"
    if state.ended_fmt:
        footer += f"  •  Ended {state.ended_fmt}"
    embed.set_footer(text=footer)
    return embed


class TimerView(discord.ui.View):
    """Pause (POI) / Resume and Skip. Only the commander or an admin may use them —
    otherwise any debater could pause or skip a speech."""

    def __init__(self, state: TimerState):
        super().__init__(timeout=None)
        self.state = state

    def disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        perms = getattr(user, "guild_permissions", None)
        ok = (
            user.id == self.state.starter.id
            or bool(perms and perms.administrator)
            or any(r.name in ADMIN_ROLE_NAMES for r in getattr(user, "roles", []))
        )
        if not ok:
            await interaction.response.send_message(
                "Only the timer's commander or an admin can use these buttons.", ephemeral=True)
        return ok

    @discord.ui.button(label="Pause (POI)", emoji="⏸", style=discord.ButtonStyle.secondary)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._allowed(interaction):
            return
        st = self.state
        if st.phase != "running":
            await interaction.response.send_message("This timer has already finished.", ephemeral=True)
            return
        if st.paused:
            st.resume()
            button.label, button.emoji = "Pause (POI)", "⏸"
        else:
            st.pause()
            button.label, button.emoji = "Resume", "▶"
        await interaction.response.edit_message(embed=render(st), view=self)

    @discord.ui.button(label="Skip", emoji="⏭", style=discord.ButtonStyle.danger)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._allowed(interaction):
            return
        st = self.state
        if st.phase != "running":
            await interaction.response.send_message("This timer has already finished.", ephemeral=True)
            return
        st.skip(interaction.user)
        self.disable_all()
        await interaction.response.edit_message(embed=render(st), view=self)


async def _pens_drop(channel, starter) -> None:
    try:
        await asyncio.sleep(PENS_DROP_DELAY)
        await edits.run(lambda: channel.send(content=f"Adjudicators Pens Drop {starter.mention}"),
                        label="timer-pens-drop")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("Could not post pens-drop message: %s", describe(e))


async def _run_countdown(
    channel: discord.TextChannel,
    starter: discord.Member,
    total_seconds: float,
    tz_offset_mins: int,
    tz_str: str,
) -> None:
    tz = timezone(timedelta(minutes=tz_offset_mins))
    started_fmt = datetime.now(tz).strftime("%d-%m-%Y %H:%M:%S")
    tz_label = _tz_label(tz_str)
    total = float(total_seconds)

    state = TimerState(channel, starter, total, started_fmt, tz_label)
    view = TimerView(state)
    state.view = view
    state.msg = await edits.run(lambda: channel.send(embed=render(state), view=view), label="timer-start")

    # One edit per whole minute / pause / resume / end, instead of one per second.
    edit_key = ("timer", channel.id, state.id)

    async def _edit() -> None:
        try:
            await state.msg.edit(embed=render(state), view=view)   # rendered when sent => never stale
        except discord.NotFound:
            state.gone = True

    # Announcements: NEW messages mentioning the commander.
    mention = starter.mention
    announcements: list[tuple[float, str]] = []
    if total > MINUTE:
        left = total - MINUTE
        if math.isclose(total, 2 * MINUTE, abs_tol=1e-6):
            announcements.append((MINUTE, f"1 Mins Done, 1 mins left {mention}"))
        else:
            announcements.append((MINUTE, f"1 Mins Done, {_mins_text(left)} left {mention}"))
            if total > 2 * MINUTE:
                announcements.append((left, f"{_mins_text(left)} done, 1 mins left {mention}"))
    marks = [k * MINUTE for k in range(1, int(total // MINUTE) + 1) if k * MINUTE < total - 1e-6]
    schedule = sorted({t for t, _ in announcements} | set(marks) | {total})
    fired: set[float] = set()
    last_minute = 0

    try:
        while state.phase == "running":
            state.wake.clear()                       # before reading state, so no wake-up is lost
            running = state.running()

            for t, text in announcements:
                if t not in fired and running >= t - 1e-6:
                    fired.add(t)
                    await edits.run(lambda tx=text: channel.send(content=tx), label="timer-announce")

            minute = int(running // MINUTE)
            if minute > last_minute:
                last_minute = minute
                timer_edits.submit(edit_key, _edit)

            if running >= total - 1e-6:
                state.phase = "ended"
                break
            if state.phase != "running":
                break

            if state.paused:
                timeout = None
            else:
                nxt = min([t for t in schedule if t > running + 1e-6] or [total])
                timeout = max(0.01, nxt - running)
            try:
                await asyncio.wait_for(state.wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            if state.gone:
                return

        state.ended_fmt = datetime.now(tz).strftime("%d-%m-%Y %H:%M:%S")
        view.disable_all()
        timer_edits.submit(edit_key, _edit)

        if state.phase == "ended":
            await edits.run(lambda: channel.send(content=f"Times up {mention}"), label="timer-times-up")
            # Independent task: starting the next timer must not swallow this message.
            task = asyncio.get_running_loop().create_task(_pens_drop(channel, starter))
            _pens_tasks.add(task)
            task.add_done_callback(_pens_tasks.discard)
        elif state.phase == "skipped":
            who = getattr(state.skipped_by, "display_name", "someone")
            await edits.run(lambda: channel.send(content=f"⏭ Timer skipped by {who} {mention}"),
                            label="timer-skipped")
    except asyncio.CancelledError:
        # A newer timer replaced this one (or the bot is stopping).
        state.phase = "replaced"
        view.disable_all()
        timer_edits.submit(edit_key, _edit)
        raise
    finally:
        if _active_timers.get(channel.id) is asyncio.current_task():
            _active_timers.pop(channel.id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(TimerCog(bot))

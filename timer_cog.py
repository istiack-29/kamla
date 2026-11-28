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
from naming import is_kind
import allin_cog

log = logging.getLogger("kamla.timer")

_active_timers: dict[int, asyncio.Task] = {}
# The starter is named in every follow-up message, but only 'Times up' actually pings them (no repeated notifications).
_QUIET = discord.AllowedMentions.none()
_pens_tasks: set[asyncio.Task] = set()

# Module constants (tests shrink them so a "minute" takes a fraction of a second).
MINUTE = 60.0
PENS_DROP_DELAY = 15.0
# The longest timer KAMLA runs. (An unbounded value used to make the countdown build a list with one entry per
# minute and froze the bot; 5 hours is also the product rule.)
MAX_SECONDS = 5 * 3600

_UNIT_WORDS = {
    "h": r"h|hr|hrs|hour|hours|houre|houres",
    "m": r"m|min|mins|minute|minutes|minuet|minuets|minut|minuts|mint|mints",
    "s": r"s|sec|secs|second|seconds|secnd|secnds",
}
# one "<digits><unit>" group; longer spellings are listed first by the alternation order below
_GROUP = re.compile(
    r"(?P<n>\d{1,6})\s*(?P<u>" + "|".join(f"(?:{w})" for w in (_UNIT_WORDS["h"], _UNIT_WORDS["m"], _UNIT_WORDS["s"])) + r")(?![a-z])",
    re.IGNORECASE)
_SEPARATORS = re.compile(r"\s*(?:,|and|&|\+)?\s*", re.IGNORECASE)


def _unit_of(word: str) -> str:
    w = word.lower()
    for key, alts in _UNIT_WORDS.items():
        if w in alts.split("|"):
            return key
    return ""


def parse_time(text: str) -> int | None:
    """Total seconds for a duration such as ``5m``, ``2 hours 30 minutes``, ``4 h 45 m`` or ``4h5min``
    (case-insensitive, flexible spacing, common spelling variants). ``None`` when it is not a duration.

    The text must be the duration ONLY: a leading activation dot is handled by :func:`classify_message`.
    Each unit may appear once; whole numbers only. Does NOT apply the 5-hour limit (the caller reports it)."""
    if not text:
        return None
    pos, total, seen = 0, 0, set()
    text = text.strip()
    while pos < len(text):
        m = _GROUP.match(text, pos)
        if m is None:
            return None
        unit = _unit_of(m.group("u"))
        if not unit or unit in seen:
            return None
        seen.add(unit)
        total += int(m.group("n")) * {"h": 3600, "m": 60, "s": 1}[unit]
        pos = m.end()
        sep = _SEPARATORS.match(text, pos)
        pos = sep.end() if sep else pos
    return total if total > 0 and seen else None


_TIMERISH = re.compile(r"^\.?\s*\d")


def classify_message(content: str, in_timer_channel: bool) -> tuple[str, int]:
    """Decide what a chat message means for the timer.

    Returns ``(kind, seconds)`` where kind is
      * ``"timer"``    – a valid timer; ``seconds`` is its length,
      * ``"too_long"`` – a valid duration above :data:`MAX_SECONDS`,
      * ``"invalid"``  – (timer channel only) it starts like a time but is not one → short help,
      * ``"ignore"``   – anything else: it is ordinary chat and must never trigger anything.

    The dot is an ACTIVATION PREFIX, never a decimal point: ``.5m`` is 5 minutes. In the dedicated timer channel
    the dot is optional (``5m`` / ``.5m``); everywhere else the message must start with one."""
    text = (content or "").strip()
    has_dot = text.startswith(".")
    if not in_timer_channel and not has_dot:
        return "ignore", 0
    body = text[1:].strip() if has_dot else text
    if body.startswith("."):                      # "..5m" is not an activation
        return ("invalid", 0) if in_timer_channel and _TIMERISH.match(text) else ("ignore", 0)
    seconds = parse_time(body)
    if seconds is None:
        if in_timer_channel and len(text) <= 40 and _TIMERISH.match(text):
            return "invalid", 0
        return "ignore", 0
    if seconds > MAX_SECONDS:
        return "too_long", seconds
    return "timer", seconds


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
    "⏱️ Send a time to start a timer: `5m` · `2h` · `4h45m` · `2 hours 30 minutes` (max 5 hours).\n"
    "In other channels start with a dot: `.5m` · `.4h45m`."
)


def title_for(total_seconds: float) -> str:
    """'5 Min', '4 Hr 45 Min', '1 Min 30 Sec', '45 Sec' — the length of the timer, for the big embed text."""
    seconds = int(round(total_seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} Hr")
    if m:
        parts.append(f"{m} Min")
    if s or not parts:
        parts.append(f"{s} Sec")
    return " ".join(parts)


def is_timer_channel(channel) -> bool:
    return isinstance(channel, discord.TextChannel) and is_kind(channel.name, "timer")


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
        """One timer for every channel — there is no separate POI feature any more.

        * In the dedicated ⏱️︱timer channel: `5m`, `2h`, `4h45m` (dot optional).
        * Anywhere else: the message must start with a dot (`.5m`, `.4h 45m`).
        * `.allin` (timer / legacy poi channels) is handed to the existing All-In implementation.
        The user's message is NEVER deleted, and anything that is not a timer is ignored."""
        if message.author.bot or not message.guild:
            return
        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return
        content = (message.content or "").strip()
        if not content:
            return

        if content.lower() == ".allin":
            await allin_cog.handle_allin_message(message)
            return

        kind, seconds = classify_message(content, is_timer_channel(channel))
        if kind == "ignore":
            return
        if kind == "invalid":
            await _say(channel, f"⚠️ {message.author.mention} that is not a valid time.\n{HELP_TEXT}", delete_after=12)
            return
        if kind == "too_long":
            await _say(channel, f"⚠️ {message.author.mention} the longest timer is {title_for(MAX_SECONDS)}.",
                       delete_after=10)
            return

        # A new timer replaces the one running in this channel.
        old = _active_timers.pop(channel.id, None)
        if old and not old.done():
            old.cancel()    # its handler marks the old message "Replaced" and disables its buttons

        cfg = await config_manager.get_config(message.guild)         # {} before installation: timers still work
        tz_str = cfg.get("timezone", "+00:00")
        tz_mins = _tz_offset_mins(tz_str)

        task = asyncio.create_task(_run_countdown(channel, message.author, seconds, tz_mins, tz_str))
        _active_timers[channel.id] = task


async def _say(channel, text: str, *, delete_after: float | None = None, **kw) -> None:
    """A transient bot reply in the channel; never raises (the channel may not allow it)."""
    try:
        await channel.send(text, delete_after=delete_after, **kw)
    except Exception as e:  # noqa: BLE001 - best effort
        log.debug("Could not post a timer notice: %s", describe(e))


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

    def __init__(self, channel, starter, total, started_fmt, tz_label, clock=time.monotonic, wall=time.time):
        TimerState._seq += 1
        self.id = TimerState._seq
        self.channel = channel
        self.starter = starter
        self.total = total
        self.started_fmt = started_fmt
        self.tz_label = tz_label
        self.ended_fmt = ""
        self._clock = clock
        self._wall = wall
        self._acc = 0.0
        self._since = clock()
        # Unix time at which the timer ends if nobody pauses. Discord draws a live countdown from it on every
        # viewer's screen (<t:..:R>), so the message needs NO extra edits to "tick". Re-anchored on resume.
        self.end_epoch = int(wall() + total)
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

    def remaining(self) -> float:
        return max(0.0, self.total - self.running())

    def resume(self) -> None:
        if self.paused and self.phase == "running":
            self._since = self._clock()
            self.paused = False
            self.end_epoch = int(self._wall() + self.remaining())
            self.wake.set()

    def skip(self, by) -> None:
        if self.phase == "running":
            self.phase = "skipped"
            self.skipped_by = by
            self.wake.set()


def render(state: TimerState) -> discord.Embed:
    """Minimal on purpose: ONE large line ("5 Min Running") and a footer with the start time and the starter.
    No title, description block or fields. The live Discord countdown (<t:..:R>, drawn by each viewer's client,
    so it costs no edits) lives in the message content, see :func:`render_content`."""
    word = {"running": "Paused" if state.paused else "Running", "ended": "Finished",
            "skipped": "Skipped", "replaced": "Replaced"}.get(state.phase, "Running")
    colors = {"Running": discord.Color.green(), "Paused": discord.Color.orange(),
              "Finished": discord.Color.dark_grey(), "Skipped": discord.Color.dark_grey(),
              "Replaced": discord.Color.dark_grey()}
    embed = discord.Embed(description=f"# {title_for(state.total * 60.0 / MINUTE)} {word}", color=colors[word])
    embed.set_footer(text=f"Started {state.started_fmt} ({state.tz_label}) • by {state.starter.display_name}")
    return embed


def render_content(state: TimerState) -> str | None:
    """The countdown line next to the embed: empty once the timer is over."""
    if state.phase != "running":
        return None
    if state.paused:
        return f"⏸ {fmt_human(int(state.remaining()))} left (paused)"
    return f"⏳ Ends <t:{state.end_epoch}:T> · <t:{state.end_epoch}:R>"


PAUSE_ID, SKIP_ID = "kamla:timer:pause", "kamla:timer:skip"


class TimerView(discord.ui.View):
    """Pause / Resume and Skip. Only the commander or an admin may use them —
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

    @discord.ui.button(label="Pause", emoji="⏸", style=discord.ButtonStyle.secondary, custom_id=PAUSE_ID)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._allowed(interaction):
            return
        st = self.state
        if st.phase != "running":
            await interaction.response.send_message("This timer has already finished.", ephemeral=True)
            return
        if st.paused:
            st.resume()
            button.label, button.emoji = "Pause", "⏸"
        else:
            st.pause()
            button.label, button.emoji = "Resume", "▶"
        await interaction.response.edit_message(content=render_content(st), embed=render(st), view=self)

    @discord.ui.button(label="Skip", emoji="⏭", style=discord.ButtonStyle.danger, custom_id=SKIP_ID)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._allowed(interaction):
            return
        st = self.state
        if st.phase != "running":
            await interaction.response.send_message("This timer has already finished.", ephemeral=True)
            return
        st.skip(interaction.user)
        self.disable_all()
        await interaction.response.edit_message(content=render_content(st), embed=render(st), view=self)


class OrphanTimerView(discord.ui.View):
    """Registered once at start-up. A timer's buttons are bound to its message while it runs; after a restart
    the countdown is gone but its buttons are still on screen — they land here instead of showing Discord's
    "This interaction failed". One user-triggered edit, nothing automatic."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _lost(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="⌛ This timer stopped because KAMLA restarted. Send the time again to start a new one.", view=None)

    @discord.ui.button(label="Pause", emoji="⏸", style=discord.ButtonStyle.secondary, custom_id=PAUSE_ID)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._lost(interaction)

    @discord.ui.button(label="Skip", emoji="⏭", style=discord.ButtonStyle.danger, custom_id=SKIP_ID)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._lost(interaction)


async def _pens_drop(channel, starter) -> None:
    try:
        await asyncio.sleep(PENS_DROP_DELAY)
        await edits.run(lambda: channel.send(content=f"Adjudicators Pens Drop {starter.mention}", allowed_mentions=_QUIET),
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
    state.msg = await edits.run(lambda: channel.send(content=render_content(state), embed=render(state), view=view),
                                label="timer-start")

    # One edit per whole minute / pause / resume / end, instead of one per second.
    edit_key = ("timer", channel.id, state.id)

    async def _edit() -> None:
        try:
            await state.msg.edit(content=render_content(state), embed=render(state), view=view)   # rendered when sent => never stale
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
                    await edits.run(lambda tx=text: channel.send(content=tx, allowed_mentions=_QUIET), label="timer-announce")

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
            await edits.run(lambda: channel.send(content=f"⏭ Timer skipped by {who} {mention}", allowed_mentions=_QUIET),
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
        view.stop()          # a finished timer's view must not stay registered in memory for ever


async def setup(bot: commands.Bot):
    bot.add_view(OrphanTimerView())
    await bot.add_cog(TimerCog(bot))

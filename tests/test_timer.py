import asyncio
import re
import types
from unittest import mock

import pytest

import timer_cog as tc


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(tc, "MINUTE", 0.1)           # a "minute" lasts 0.1 s
    monkeypatch.setattr(tc, "PENS_DROP_DELAY", 0.05)
    monkeypatch.setattr(tc, "_active_timers", {})
    # pause the real-minute formatting of the total
    monkeypatch.setattr(tc, "fmt_human", lambda s: f"{s}s")


class Msg:
    def __init__(self, log):
        self.log = log
        self.embeds = []

    async def edit(self, content=None, embed=None, view=None):
        self.embeds.append(embed)


class Chan:
    id = 5

    def __init__(self):
        self.sent = []            # plain-content messages, in order
        self.msgs = []

    async def send(self, content=None, embed=None, view=None, **kw):
        if embed is None and content is not None:       # announcements; the timer message itself carries an embed
            self.sent.append(content)
        m = Msg(self.sent)
        m.first_embed, m.view = embed, view
        m.first_content = content
        self.msgs.append(m)
        return m


def commander(uid=1):
    return types.SimpleNamespace(id=uid, display_name="Cmd", mention="@Cmd")


async def ready(ch):
    """The first message goes through the shared paced coordinator; wait until it was sent."""
    for _ in range(200):
        if ch.msgs:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timer message never sent")


def start(total, ch=None, cmd=None):
    ch = ch or Chan()
    cmd = cmd or commander()
    task = asyncio.create_task(tc._run_countdown(ch, cmd, total, 0, "+00:00"))
    tc._active_timers[ch.id] = task
    return ch, task


async def test_announcements_in_order_and_exact_wording():
    ch, task = start(0.5)                                  # a "5 minute" timer
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.12)                             # let the pens-drop task fire
    assert ch.sent == [
        "1 Mins Done, 4 mins left @Cmd",
        "4 mins done, 1 mins left @Cmd",
        "Times up @Cmd",
        "Adjudicators Pens Drop @Cmd",
    ]


async def test_status_embed_is_one_large_line_and_a_footer():
    ch, task = start(0.3)                                  # a "3 minute" timer
    await ready(ch)
    first = ch.msgs[0].first_embed
    assert first.description == "# 3 Min Running"
    assert not first.title and not first.fields           # no title, no fields
    assert first.footer.text.endswith("by Cmd") and first.footer.text.startswith("Started ")
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.15)
    assert ch.msgs[0].embeds[-1].description == "# 3 Min Finished"
    for e in [first, *ch.msgs[0].embeds]:
        assert not re.search(r"\d+:\d{2}", e.description or "")      # never a m:ss countdown


async def test_short_timers_do_not_spam():
    ch, task = start(0.08)                                 # <= 1 minute: only Times up
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.12)
    assert ch.sent == ["Times up @Cmd", "Adjudicators Pens Drop @Cmd"]
    ch2, task2 = start(0.2)                                # exactly 2 minutes: one combined message
    ch2.id = 6
    await asyncio.wait_for(task2, 5)
    await asyncio.sleep(0.12)
    assert ch2.sent[0] == "1 Mins Done, 1 mins left @Cmd" and len(ch2.sent) == 3


async def test_pause_stops_the_clock_and_resume_continues():
    ch, task = start(0.3)
    await ready(ch)
    await asyncio.sleep(0.05)
    view = ch.msgs[0].view
    inter = mock.MagicMock()
    inter.user = commander()
    inter.response.edit_message = mock.AsyncMock()
    t0 = asyncio.get_event_loop().time()
    await view.pause_btn.callback(inter)
    ran = view.state.running()                             # clock value frozen at pause time
    assert view.state.paused and view.pause_btn.label == "Resume"
    assert "Paused" in inter.response.edit_message.await_args.kwargs["embed"].description
    await asyncio.sleep(0.28)                             # past the unpaused end (timer had ~0.25 s left)
    assert not task.done() and "Times up @Cmd" not in ch.sent
    await view.pause_btn.callback(inter)
    assert not view.state.paused
    await asyncio.wait_for(task, 5)
    assert view.state.running() == pytest.approx(0.3, abs=0.02)
    assert asyncio.get_event_loop().time() - t0 >= 0.28 + (0.3 - ran) - 0.03  # paused time did not count
    assert "Times up @Cmd" in ch.sent


async def test_skip_ends_immediately_without_times_up_or_pens_drop():
    ch, task = start(0.5)
    await ready(ch)
    view = ch.msgs[0].view
    inter = mock.MagicMock()
    inter.user = commander()
    inter.response.edit_message = mock.AsyncMock()
    await view.skip_btn.callback(inter)
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.15)
    assert ch.sent == ["⏭ Timer skipped by Cmd @Cmd"]
    assert all(c.disabled for c in view.children)


async def test_buttons_restricted_to_commander_or_admin():
    ch, task = start(0.5)
    await ready(ch)
    view = ch.msgs[0].view

    def user(uid, admin=False, role=None):
        u = types.SimpleNamespace(id=uid, display_name="x",
                                  guild_permissions=types.SimpleNamespace(administrator=admin),
                                  roles=[types.SimpleNamespace(name=role)] if role else [])
        i = mock.MagicMock()
        i.user = u
        i.response.send_message = mock.AsyncMock()
        i.response.edit_message = mock.AsyncMock()
        return i

    stranger = user(99)
    await view.skip_btn.callback(stranger)
    stranger.response.send_message.assert_awaited()
    assert view.state.phase == "running"

    tabby = user(98, role="TABBY")
    await view.pause_btn.callback(tabby)
    assert view.state.paused
    task.cancel()


async def test_replaced_timer_marks_old_message_and_leaves_no_slot():
    ch, task = start(0.5)
    await ready(ch)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if ch.msgs[0].embeds:
            break
        await asyncio.sleep(0.01)
    assert "Replaced" in ch.msgs[0].embeds[-1].description
    assert tc._active_timers.get(ch.id) is None


def test_mins_text():
    assert tc._mins_text(0.4) == "4 mins"
    assert tc._mins_text(0.15) in ("1m 30s", "1m 29s", "1m 31s")


# ── an absurd length must not freeze the bot ──────────────────────────────────

def _timer_message(content):
    import discord
    chan = mock.MagicMock(spec=discord.TextChannel)
    chan.name, chan.id = "timer", 77
    chan.send = mock.AsyncMock()
    msg = mock.MagicMock()
    msg.author.bot, msg.author.mention, msg.guild, msg.channel, msg.content = False, "@u", types.SimpleNamespace(id=9), chan, content
    msg.delete = mock.AsyncMock()
    return msg, chan


@pytest.mark.parametrize("text", ["6h", "5h 1s", "18001s", "99999h", ".5h 1 min"])
async def test_too_long_a_timer_is_refused_and_nothing_starts(text):
    cog = tc.TimerCog(bot=None)
    msg, chan = _timer_message(text)
    await cog.on_message(msg)
    assert chan.send.await_count == 1 and "longest timer" in chan.send.await_args.args[0]
    assert 77 not in tc._active_timers


async def test_five_hours_is_the_longest_allowed(monkeypatch):
    from tests.fakes import FakeConfig
    monkeypatch.setattr(tc, "config_manager", FakeConfig())
    started = []

    async def fake_run(channel, starter, total, *a):
        started.append(total)
        await asyncio.sleep(10)
    monkeypatch.setattr(tc, "_run_countdown", fake_run)
    cog = tc.TimerCog(bot=None)
    msg, chan = _timer_message("5h")
    await cog.on_message(msg)
    await asyncio.sleep(0.01)
    assert started == [18000] and 77 in tc._active_timers
    assert not msg.delete.await_count                       # the user's message is never deleted
    tc._active_timers[77].cancel()


def test_the_longest_timer_only_has_a_small_schedule():
    total = float(tc.MAX_SECONDS)
    marks = [k * 60.0 for k in range(1, int(total // 60.0) + 1) if k * 60.0 < total - 1e-6]
    assert len(marks) < 1500                                           # not millions


# ── live countdown without extra edits, progress bar, restart-proof buttons ───

def fake_state(total=600, now=1_000_000.0):
    t = {"clock": 0.0, "wall": now}
    st = tc.TimerState(Chan(), commander(), total, "01-01-2026 10:00:00", "GMT +6",
                       clock=lambda: t["clock"], wall=lambda: t["wall"])
    return st, t


def field(embed, name):
    return next((f.value for f in embed.fields if f.name == name), None)


@pytest.fixture
def real_minute(monkeypatch):
    monkeypatch.setattr(tc, "MINUTE", 60.0)


def test_running_timer_shows_a_live_discord_countdown_next_to_the_minimal_embed(real_minute):
    st, t = fake_state(600)
    e = tc.render(st)
    assert e.description == "# 10 Min Running" and not e.fields and not e.title
    assert tc.render_content(st) == "⏳ Ends <t:1000600:T> · <t:1000600:R>"      # Discord renders the countdown itself


def test_the_text_never_contains_a_ticking_clock(real_minute):
    st, t = fake_state(125)
    t["clock"] = 61.0
    e = tc.render(st)
    assert not re.search(r"\d+:\d{2}", e.description)


def test_paused_shows_static_time_left_and_resume_re_anchors_the_end(real_minute):
    st, t = fake_state(600)
    t["clock"] = 100.0
    st.pause()
    assert tc.render(st).description == "# 10 Min Paused"
    assert tc.render_content(st).startswith("⏸") and "paused" in tc.render_content(st)
    t["clock"], t["wall"] = 400.0, 1_000_400.0                          # five minutes of pause go by
    st.resume()
    assert st.end_epoch == 1_000_400 + 500                              # 500 s were left when it was resumed
    assert "<t:1000900:R>" in tc.render_content(st)


def test_finished_timer_has_no_countdown(real_minute):
    st, _ = fake_state(60)
    st.phase = "ended"
    assert tc.render(st).description == "# 1 Min Finished" and tc.render_content(st) is None


async def test_the_countdown_costs_no_more_edits_than_before():
    ch, task = start(0.5)                                                 # five "minutes"
    await ready(ch)
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.15)
    assert len(ch.msgs[0].embeds) <= 7                                    # ~ one per minute + end; never one per second


async def test_live_buttons_use_fixed_ids_so_they_survive_a_restart():
    st, _ = fake_state(60)
    view = tc.TimerView(st)
    assert sorted(b.custom_id for b in view.children) == sorted([tc.PAUSE_ID, tc.SKIP_ID])


@pytest.mark.parametrize("button", ["pause_btn", "skip_btn"])
async def test_buttons_of_a_timer_lost_in_a_restart_explain_themselves(button):
    view = tc.OrphanTimerView()
    i = mock.MagicMock()
    i.response.edit_message = mock.AsyncMock()
    await getattr(view, button).callback(i)
    kwargs = i.response.edit_message.await_args.kwargs
    assert "restarted" in kwargs["content"] and kwargs["view"] is None
    assert {b.custom_id for b in view.children} == {tc.PAUSE_ID, tc.SKIP_ID}
    assert view.is_persistent()


async def test_setup_registers_the_fallback_view():
    bot = mock.MagicMock()
    bot.add_cog = mock.AsyncMock()
    await tc.setup(bot)
    assert isinstance(bot.add_view.call_args.args[0], tc.OrphanTimerView)


async def test_a_finished_timers_view_is_released():
    ch, task = start(0.05)
    await ready(ch)
    await asyncio.wait_for(task, 5)
    assert ch.msgs[0].view.is_finished()

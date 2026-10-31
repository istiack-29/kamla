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

    async def edit(self, embed=None, view=None):
        self.embeds.append(embed)


class Chan:
    id = 5

    def __init__(self):
        self.sent = []            # plain-content messages, in order
        self.msgs = []

    async def send(self, content=None, embed=None, view=None):
        if content is not None:
            self.sent.append(content)
        m = Msg(self.sent)
        m.first_embed, m.view = embed, view
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
    await asyncio.sleep(0.4)                               # let the pens-drop task fire
    assert ch.sent == [
        "1 Mins Done, 4 mins left @Cmd",
        "4 mins done, 1 mins left @Cmd",
        "Times up @Cmd",
        "Adjudicators Pens Drop @Cmd",
    ]


async def test_status_embed_has_no_clock_and_counts_minutes():
    ch, task = start(0.3)
    await ready(ch)
    assert "0 min running" in ch.msgs[0].first_embed.description
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.3)
    texts = [e.description for e in ch.msgs[0].embeds]
    assert any("1 min running" in t for t in texts)
    assert texts[-1] == "✅ **Time's up**"
    for e in [ch.msgs[0].first_embed, *ch.msgs[0].embeds]:
        assert not re.search(r"\d+:\d{2}", e.description or "")      # never a m:ss countdown
        assert not (e.title or "").startswith("⏱  ")


async def test_short_timers_do_not_spam():
    ch, task = start(0.08)                                 # <= 1 minute: only Times up
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.2)
    assert ch.sent == ["Times up @Cmd", "Adjudicators Pens Drop @Cmd"]
    ch2, task2 = start(0.2)                                # exactly 2 minutes: one combined message
    ch2.id = 6
    await asyncio.wait_for(task2, 5)
    await asyncio.sleep(0.2)
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
    await asyncio.sleep(0.4)                               # way past the original end
    assert not task.done() and "Times up @Cmd" not in ch.sent
    await view.pause_btn.callback(inter)
    assert not view.state.paused
    await asyncio.wait_for(task, 5)
    assert view.state.running() == pytest.approx(0.3, abs=0.02)
    assert asyncio.get_event_loop().time() - t0 >= 0.4 + (0.3 - ran) - 0.03   # paused time did not count
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
    await asyncio.sleep(0.3)
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

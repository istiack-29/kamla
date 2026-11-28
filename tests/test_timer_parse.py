"""Unified timer: duration parsing, the dot prefix, any-channel activation, All-In dispatch, nothing deleted."""
import asyncio
import types
from unittest import mock

import discord
import pytest

import allin_cog
import timer_cog as tc
from naming import CH_TIMER


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(tc, "_active_timers", {})


VALID = {
    "5s": 5, "5 sec": 5, "5 seconds": 5, "5 SECS": 5,
    "5m": 300, "5min": 300, "5 minute": 300, "5 minutes": 300, "5 MIN": 300, "5 Minutes": 300,
    "2h": 7200, "2 hours": 7200, "2 HR": 7200, "1 hour": 3600,
    "4h45m": 4 * 3600 + 45 * 60, "4 h 45 m": 4 * 3600 + 45 * 60, "4h 5min": 4 * 3600 + 300,
    "2 hours 30 minutes": 9000, "1h 1m 1s": 3661, "2H30M": 9000, "90 mins": 5400, "5   m": 300,
    "1 hour and 5 minutes": 3900, "2h, 30m": 9000,
}


@pytest.mark.parametrize("text,seconds", VALID.items())
def test_every_documented_format_parses(text, seconds):
    assert tc.parse_time(text) == seconds


@pytest.mark.parametrize("text", ["", "hello", "5", "5x", "5.5m", "5m5m", "m5", "5 minutes later", "0m", "-5m", "5d"])
def test_things_that_are_not_durations(text):
    assert tc.parse_time(text) is None


@pytest.mark.parametrize("text", [".5m", ".5 m", ".5min", ".5minute", ".5minutes", ".5 min", ".5 minute", ".5 minutes"])
@pytest.mark.parametrize("in_timer_channel", [True, False])
def test_the_dot_is_an_activation_prefix_not_a_decimal_point(text, in_timer_channel):
    # ".5m" is FIVE minutes, never half a minute, in every channel
    assert tc.classify_message(text, in_timer_channel) == ("timer", 300)


@pytest.mark.parametrize("text,seconds", [(".4h", 14400), (".4h45m", 4 * 3600 + 2700), (".4 h 45 m", 4 * 3600 + 2700)])
def test_dotted_hours_anywhere(text, seconds):
    assert tc.classify_message(text, False) == ("timer", seconds)


def test_dedicated_channel_needs_no_dot_other_channels_do():
    for t in ("5m", "2h", "4h45m"):
        assert tc.classify_message(t, True)[0] == "timer"
        assert tc.classify_message(t, False)[0] == "ignore"


def test_unrelated_chat_never_triggers_anything():
    for t in ("hello everyone", "gg", "I have 5 minutes left", "5 people joined", ".gg", "...", ".", "ok.5m"):
        assert tc.classify_message(t, False)[0] == "ignore"
    assert tc.classify_message("hello everyone", True)[0] == "ignore"
    assert tc.classify_message("..5m", False)[0] == "ignore"


def test_max_duration_is_five_hours():
    assert tc.MAX_SECONDS == 5 * 3600
    assert tc.classify_message("5h", True) == ("timer", 18000)
    assert tc.classify_message("5h 1s", True)[0] == "too_long"
    assert tc.classify_message(".301m", False)[0] == "too_long"


def test_a_timer_like_typo_in_the_timer_channel_gets_help_elsewhere_silence():
    assert tc.classify_message("5 mnths", True)[0] == "invalid"
    assert tc.classify_message(".5 mnths", False)[0] == "ignore"


@pytest.mark.parametrize("seconds,title", [(300, "5 Min"), (45, "45 Sec"), (7200, "2 Hr"), (17100, "4 Hr 45 Min"),
                                           (90, "1 Min 30 Sec"), (18000, "5 Hr")])
def test_embed_title_is_dynamic(seconds, title):
    assert tc.title_for(seconds) == title


# ── on_message behaviour ───────────────────────────────────────────────────────────────────────────────────

def message(content, channel_name, *, roles=(), category=None):
    chan = mock.MagicMock(spec=discord.TextChannel)
    chan.name, chan.id, chan.category = channel_name, abs(hash(channel_name)) % 10_000, category
    chan.send = mock.AsyncMock()
    msg = mock.MagicMock()
    msg.author = types.SimpleNamespace(bot=False, id=7, mention="@u", display_name="u",
                                       roles=[types.SimpleNamespace(name=r) for r in roles])
    msg.guild = types.SimpleNamespace(id=9, owner_id=1)
    msg.channel, msg.content = chan, content
    msg.delete = mock.AsyncMock()
    return msg, chan


@pytest.fixture
def started(monkeypatch):
    from tests.fakes import FakeConfig
    monkeypatch.setattr(tc, "config_manager", FakeConfig())     # no installation at all: timers must still work
    calls = []

    async def fake_run(channel, starter, total, *a):
        calls.append((channel.name, total))
        await asyncio.sleep(5)
    monkeypatch.setattr(tc, "_run_countdown", fake_run)
    yield calls
    for t in list(tc._active_timers.values()):
        t.cancel()


@pytest.mark.parametrize("name", ["general", "📢︱chat", CH_TIMER, "poi"])
async def test_dotted_timer_works_in_any_channel_before_installation(started, name):
    msg, chan = message(".5m", name)
    await tc.TimerCog(bot=None).on_message(msg)
    await asyncio.sleep(0.01)
    assert started == [(name, 300)]
    msg.delete.assert_not_awaited()                              # the user's message is never deleted


async def test_undotted_timer_only_in_the_timer_channel(started):
    for name, expect in ((CH_TIMER, True), ("timer", True), ("general", False), ("poi", False)):
        started.clear()
        tc._active_timers.clear()
        msg, _ = message("4h45m", name)
        await tc.TimerCog(bot=None).on_message(msg)
        await asyncio.sleep(0.01)
        assert bool(started) is expect, name
        msg.delete.assert_not_awaited()


async def test_junk_in_the_timer_channel_breaks_nothing_and_is_not_deleted(started):
    cog = tc.TimerCog(bot=None)
    for text in ("hello", "🎉🎉", "5 mnths please", "..5m", "", "   ", "x" * 3000):
        msg, chan = message(text, CH_TIMER)
        await cog.on_message(msg)
        msg.delete.assert_not_awaited()
    assert started == []


async def test_new_timer_replaces_the_one_running_in_that_channel(started):
    cog = tc.TimerCog(bot=None)
    m1, chan = message("5m", CH_TIMER)
    await cog.on_message(m1)
    first = tc._active_timers[chan.id]
    m2, _ = message("3m", CH_TIMER)
    m2.channel = chan
    await cog.on_message(m2)
    await asyncio.sleep(0.01)
    assert first.cancelled() or first.done()
    assert len(tc._active_timers) == 1


def test_there_is_no_poi_feature_left():
    import importlib.util, pathlib
    assert importlib.util.find_spec("poi_cog") is None
    src = pathlib.Path(tc.__file__).read_text(encoding="utf-8")
    assert "poi_cog" not in src and "(POI)" not in src


# ── .allin ──────────────────────────────────────────────────────────────────────────────────────────────────

def room_category(prep_members=2):
    prep = mock.MagicMock(); prep.name = "🔊︱OG PREP"
    members = []
    for _ in range(prep_members):
        m = mock.MagicMock(); m.move_to = mock.AsyncMock(); members.append(m)
    prep.members = members
    debate = mock.MagicMock(); debate.name = "🎤︱DEBATE ROOM"; debate.members = []
    adj = mock.MagicMock(); adj.name = "⚖️︱ADJUDICATION"; adj.members = [mock.MagicMock()]
    cat = mock.MagicMock(spec=discord.CategoryChannel)
    cat.id = 555
    cat.voice_channels = [debate, prep, adj]
    return cat, members, debate


@pytest.fixture(autouse=True)
def allin_state(monkeypatch):
    monkeypatch.setattr(allin_cog, "_running", set())
    monkeypatch.setattr(allin_cog, "_last_run", {})
    from ratelimit import RequestCoordinator
    async def nosleep(_): return None
    monkeypatch.setattr(allin_cog, "notify", RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep, "rng": lambda: 0.0}))


@pytest.mark.parametrize("roles,allowed", [
    (["ORG"], True), (["CAP"], True), (["TABBY"], True), (["EQUITY"], True), (["INVITED ADJUDICATOR"], True),
    (["Some Custom Role"], True),
    (["VISITOR"], False), (["INDEPENDENT ADJUDICATOR"], False), (["DEBATER"], False),
    (["ORG", "DEBATER"], False), (["Some Custom Role", "visitor"], False),
    ([], False),
])
def test_allin_is_role_based(roles, allowed):
    member = types.SimpleNamespace(id=5, roles=[types.SimpleNamespace(name=n) for n in ["@everyone", *roles]],
                                   guild=types.SimpleNamespace(owner_id=1), guild_permissions=types.SimpleNamespace(administrator=False))
    assert allin_cog.can_use_allin(member) is allowed


async def test_dot_allin_in_a_timer_channel_moves_prep_members_and_keeps_the_message():
    cat, members, debate = room_category()
    msg, chan = message(".allin", CH_TIMER, roles=["TABBY"], category=cat)
    await tc.TimerCog(bot=None).on_message(msg)
    for m in members:
        m.move_to.assert_awaited_once()
        assert m.move_to.await_args.args[0] is debate
    msg.delete.assert_not_awaited()
    assert "Moved **2**" in chan.send.await_args.args[0]


async def test_dot_allin_also_works_in_a_legacy_poi_channel_and_any_category():
    cat, members, _ = room_category(3)
    msg, chan = message(".AllIn", "poi", roles=["ORG"], category=cat)
    await tc.TimerCog(bot=None).on_message(msg)
    assert all(m.move_to.await_count == 1 for m in members)


@pytest.mark.parametrize("role", ["VISITOR", "INDEPENDENT ADJUDICATOR", "DEBATER"])
async def test_excluded_roles_cannot_trigger_allin(role):
    cat, members, _ = room_category()
    msg, chan = message(".allin", CH_TIMER, roles=[role], category=cat)
    await tc.TimerCog(bot=None).on_message(msg)
    assert all(m.move_to.await_count == 0 for m in members)
    assert "cannot use" in chan.send.await_args.args[0]
    msg.delete.assert_not_awaited()


async def test_allin_in_another_channel_does_nothing():
    cat, members, _ = room_category()
    msg, chan = message(".allin", "general", roles=["ORG"], category=cat)
    await tc.TimerCog(bot=None).on_message(msg)
    assert all(m.move_to.await_count == 0 for m in members)
    chan.send.assert_not_awaited()


async def test_allin_does_not_start_a_timer_and_uses_the_one_shared_implementation(monkeypatch, started):
    cat, _, _ = room_category()
    seen = []

    async def fake_push(category):
        seen.append(category)
        return allin_cog.PushResult(moved=0, debate_room=mock.MagicMock(name="d"))
    monkeypatch.setattr(allin_cog, "push_back_all", fake_push)
    msg, _ = message(".allin", CH_TIMER, roles=["ORG"], category=cat)
    await tc.TimerCog(bot=None).on_message(msg)
    assert seen == [cat] and started == []
    # the legacy button calls the very same function
    import inspect
    assert "push_back_all(" in inspect.getsource(allin_cog.AllInView.push_back_all_btn)


async def test_double_allin_is_not_run_twice():
    cat, members, _ = room_category()
    msg, chan = message(".allin", CH_TIMER, roles=["ORG"], category=cat)
    cog = tc.TimerCog(bot=None)
    await cog.on_message(msg)
    await cog.on_message(msg)
    assert members[0].move_to.await_count == 1

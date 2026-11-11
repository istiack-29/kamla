import asyncio
import time
import types
from unittest import mock

import discord
import pytest
from aiohttp.test_utils import TestServer

import tabbycat_cog as cog_mod
from ratelimit import RequestCoordinator
from tabbycat_client import TabbycatClient
from tests.fakes import FakeConfig
from tests.test_tabbycat_client import build_app


@pytest.fixture
async def env(monkeypatch):
    app, state = build_app()
    srv = TestServer(app)
    await srv.start_server()

    async def nosleep(_):
        return None
    fast = RequestCoordinator("t", 4, 0, retry_kwargs={"sleep": nosleep})
    monkeypatch.setattr(cog_mod, "notify", fast)
    cfg = FakeConfig()
    monkeypatch.setattr(cog_mod, "config_manager", cfg)
    monkeypatch.setattr(cog_mod, "validate_base_url", lambda u, **k: u)            # the test server is plain http on 127.0.0.1
    monkeypatch.setattr(cog_mod, "_new_client", lambda u, s, t: TabbycatClient(u, s, t, allow_insecure=True))
    cog = cog_mod.TabbycatCog(bot=None)
    yield types.SimpleNamespace(srv=srv, cfg=cfg, cog=cog, state=state)
    await cog.cog_unload()
    await srv.close()


_ids = iter(range(5000, 9000))


class FakeGuild:
    """Just what the cog touches: an id, an owner and a writable list of text channels."""

    def __init__(self):
        self.id = next(_ids)
        self.owner_id = 4242
        self.text_channels = []


class Chan:
    def __init__(self, name):
        self.name, self.mention, self.sent = name, f"#{name}", []

    async def send(self, content=None, files=None, **kw):
        self.sent.append({"content": content, "files": files or []})


def member(*roles, admin=False, uid=1, owner=False):
    guild_owner = uid if owner else 4242
    return types.SimpleNamespace(
        id=uid, roles=[types.SimpleNamespace(name=r) for r in roles],
        guild_permissions=types.SimpleNamespace(administrator=admin),
        guild=types.SimpleNamespace(owner_id=guild_owner))


def interaction(user, guild=None, channel=None):
    guild = guild or FakeGuild()
    matchup, motion, ballot, brk = (Chan("🎓︱matchup"), Chan("🎓︱motion"), Chan("🎓︱ballot"), Chan("🎓︱break"))
    guild.text_channels = [matchup, motion, ballot, brk]
    guild.channels_by = {"draw": matchup, "motion": motion, "result": ballot, "break": brk}
    i = mock.MagicMock()
    i.user, i.guild = user, guild
    i.channel = channel or Chan("general")
    i.response.defer = mock.AsyncMock()
    i.response.send_message = mock.AsyncMock()
    i.response.send_modal = mock.AsyncMock()
    i.followup.send = mock.AsyncMock()
    i.response.edit_message = mock.AsyncMock()
    i.edit_original_response = mock.AsyncMock()
    return i


def last_text(i):
    call = i.followup.send.await_args or i.response.send_message.await_args
    return (call.args[0] if call.args else call.kwargs.get("content", "")) or ""


async def connect(env, guild):
    i = interaction(member("TABBY"), guild)
    await env.cog.connect(i, f"http://127.0.0.1:{env.srv.port}", "kijani", "secret-token")
    return i


# ── permissions ───────────────────────────────────────────────────────────────

def test_role_rules():
    assert cog_mod.is_tab_admin(member("TABBY")) and cog_mod.is_tab_admin(member(admin=True)) and cog_mod.is_tab_admin(member(owner=True))
    assert not cog_mod.is_tab_admin(member("CAP")) and not cog_mod.is_tab_admin(member("DEBATER"))
    for role in ("CAP", "ORG", "TABBY"):
        assert cog_mod.is_operator(member(role))
    assert not cog_mod.is_operator(member("EQUITY")) and not cog_mod.is_operator(member("DEBATER", "VISITOR"))


async def test_tab_command_is_for_tabby_only(env):
    cog = env.cog
    for who in (member("ORG"), member("DEBATER")):
        i = interaction(who)
        await cog.tab.callback(cog, i, None)
        assert "TABBY" in last_text(i) and not i.response.send_modal.await_count
    i = interaction(member("TABBY"))
    await cog.tab.callback(cog, i, None)
    assert i.response.send_modal.await_count == 1


async def test_other_commands_refuse_non_operators(env):
    cog = env.cog
    for name, args in (("draw", (1,)), ("motion", (1,)), ("result", (1,)), ("break_", ("Open",)), ("refresh", ())):
        i = interaction(member("DEBATER"))
        await getattr(cog, name).callback(cog, i, *args)
        assert "CAP, ORG and TABBY" in last_text(i)


# ── connecting ────────────────────────────────────────────────────────────────

async def test_connect_saves_validates_and_caches(env):
    guild = FakeGuild()
    i = await connect(env, guild)
    saved = env.cfg.data[guild.id]["tabbycat"]
    assert saved["slug"] == "kijani" and saved["token"] == "secret-token"
    embed = i.followup.send.await_args.kwargs["embed"]
    assert "Connected to Kijani Open" in embed.title and "Round 1" in embed.description
    assert env.cog._cache[guild.id].tournament == "Kijani"
    assert env.cfg.compacted == 1                                     # token changed -> old config copies purged


async def test_connect_failures_are_friendly_and_nothing_is_saved(env):
    guild = FakeGuild()
    i = interaction(member("TABBY"), guild)
    await env.cog.connect(i, f"http://127.0.0.1:{env.srv.port}", "kijani", "WRONG")
    assert "refused access" in last_text(i) and "WRONG" not in last_text(i)
    await env.cog.connect(i, f"http://127.0.0.1:{env.srv.port}", "bad slug!", "secret-token")
    assert "slug" in last_text(i)
    assert guild.id not in env.cfg.data


async def test_reconnect_with_empty_token_keeps_the_saved_one(env):
    guild = FakeGuild()
    await connect(env, guild)
    i = interaction(member("TABBY"), guild)
    await env.cog.connect(i, f"http://127.0.0.1:{env.srv.port}", "kijani", "")
    assert env.cfg.data[guild.id]["tabbycat"]["token"] == "secret-token"
    assert env.cfg.compacted == 1                                     # token unchanged -> no second purge


async def test_disconnect_removes_token_and_cache(env):
    guild = FakeGuild()
    await connect(env, guild)
    i = interaction(member("TABBY"), guild)
    await env.cog.tab.callback(env.cog, i, types.SimpleNamespace(value="disconnect"))
    assert "tabbycat" not in env.cfg.data[guild.id] and guild.id not in env.cog._cache and env.cfg.compacted == 2


async def test_status_never_shows_the_token(env):
    guild = FakeGuild()
    await connect(env, guild)
    i = interaction(member("TABBY"), guild)
    await env.cog.tab.callback(env.cog, i, types.SimpleNamespace(value="status"))
    embed = i.response.send_message.await_args.kwargs["embed"]
    dump = " ".join([embed.title or ""] + [f.name + f.value for f in embed.fields])
    assert "secret-token" not in dump and "saved" in dump


# ── options (autocomplete) ────────────────────────────────────────────────────

async def choices(env, fn, guild, current=""):
    i = interaction(member("CAP"), guild)
    return await fn(env.cog, i, current)


async def test_autocomplete_states(env):
    guild = FakeGuild()
    out = await choices(env, cog_mod.TabbycatCog._draw_ac, guild)
    assert out[0].value == cog_mod.NONE_VALUE and "/tab" in out[0].name              # not connected
    await connect(env, guild)
    env.cog._cache.pop(guild.id)
    out = await choices(env, cog_mod.TabbycatCog._draw_ac, guild)
    assert out[0].name == "Round 1"                                                  # connected, nothing cached → loaded on the spot
    draw = await choices(env, cog_mod.TabbycatCog._draw_ac, guild)
    assert [c.name for c in draw[:-1]] == ["Round 1", "Round 3"]                  # only rounds whose draw is released
    assert [c.value for c in draw[:-1]] == [1, 3]
    assert "/refresh" in draw[-1].name and draw[-1].value == cog_mod.NONE_VALUE   # warning is always the last line
    motion = await choices(env, cog_mod.TabbycatCog._motion_ac, guild)
    assert [c.name for c in motion[:-1]] == ["Round 1"] and "/refresh" in motion[-1].name
    result = await choices(env, cog_mod.TabbycatCog._result_ac, guild)
    assert [c.name for c in result[:-1]] == ["Round 1"]
    assert [c.name for c in await choices(env, cog_mod.TabbycatCog._break_ac, guild)] == ["Open"]
    assert [c.name for c in (await choices(env, cog_mod.TabbycatCog._draw_ac, guild, "3"))[:-1]] == ["Round 3"]


def counting_refresh(env, monkeypatch, delay=0.0):
    calls = []
    real = env.cog._refresh

    async def wrapper(guild):
        calls.append(guild.id)
        if delay:
            await asyncio.sleep(delay)
        return await real(guild)
    monkeypatch.setattr(env.cog, "_refresh", wrapper)
    return calls


async def test_dropdown_reloads_stale_data_but_not_fresh_data(env, monkeypatch):
    guild = FakeGuild()
    await connect(env, guild)
    calls = counting_refresh(env, monkeypatch)
    cache = env.cog._cache[guild.id]
    for r in cache.rounds:                                             # pretend Tabbycat changed since we cached it
        r.motions_released = False
    out = await choices(env, cog_mod.TabbycatCog._motion_ac, guild)
    assert not calls and len(out) == 1                                 # fresh cache → no request, nothing released
    cache.fetched -= cog_mod.AUTO_TTL + 1
    out = await choices(env, cog_mod.TabbycatCog._motion_ac, guild)
    assert len(calls) == 1 and out[0].name == "Round 1"                # stale → re-read, the released round shows up
    for fn in (cog_mod.TabbycatCog._draw_ac, cog_mod.TabbycatCog._result_ac, cog_mod.TabbycatCog._break_ac):
        env.cog._cache[guild.id].fetched -= cog_mod.AUTO_TTL + 1       # every dropdown refreshes, not only /motion and /draw
        await choices(env, fn, guild)
    assert len(calls) == 4


async def test_many_dropdowns_at_once_share_one_refresh(env, monkeypatch):
    guild = FakeGuild()
    await connect(env, guild)
    calls = counting_refresh(env, monkeypatch, delay=0.05)
    env.cog._cache[guild.id].fetched -= cog_mod.AUTO_TTL + 1
    outs = await asyncio.gather(*(choices(env, cog_mod.TabbycatCog._draw_ac, guild) for _ in range(8)))
    assert len(calls) == 1 and all(o[0].name == "Round 1" for o in outs)


async def test_slow_refresh_never_blocks_the_dropdown_and_finishes_in_background(env, monkeypatch):
    guild = FakeGuild()
    await connect(env, guild)
    monkeypatch.setattr(cog_mod, "AUTOCOMPLETE_WAIT", 0.05)
    counting_refresh(env, monkeypatch, delay=0.3)
    cache = env.cog._cache[guild.id]
    cache.fetched -= cog_mod.AUTO_TTL + 1
    t0 = time.monotonic()
    out = await choices(env, cog_mod.TabbycatCog._draw_ac, guild)
    assert time.monotonic() - t0 < 0.25 and out[0].name == "Round 1"   # answered from the old data, quickly
    await asyncio.sleep(0.6)
    assert env.cog._cache[guild.id] is not cache                       # the refresh finished by itself


async def test_site_down_dropdown_uses_old_data_and_backs_off(env, monkeypatch):
    guild = FakeGuild()
    await connect(env, guild)
    await env.srv.close()
    calls = counting_refresh(env, monkeypatch)
    env.cog._cache[guild.id].fetched -= cog_mod.AUTO_TTL + 1
    for _ in range(3):
        out = await choices(env, cog_mod.TabbycatCog._draw_ac, guild)
        assert out[0].name == "Round 1"                                # still usable from the last good data
    assert len(calls) == 1                                             # no hammering of a site that is down


async def test_site_down_and_no_data_shows_a_refresh_warning(env):
    guild = FakeGuild()
    await connect(env, guild)
    await env.srv.close()
    env.cog._cache.pop(guild.id)
    out = await choices(env, cog_mod.TabbycatCog._draw_ac, guild)
    assert len(out) == 1 and "/refresh" in out[0].name


async def test_autocomplete_none_released_only_shows_the_refresh_warning(env):
    guild = FakeGuild()
    await connect(env, guild)
    env.cog._cache[guild.id].rounds[:] = [r for r in env.cog._cache[guild.id].rounds if r.seq == 2]    # only a draft round
    for fn in (cog_mod.TabbycatCog._draw_ac, cog_mod.TabbycatCog._motion_ac, cog_mod.TabbycatCog._result_ac):
        out = await choices(env, fn, guild)
        assert len(out) == 1 and "No " in out[0].name and "please /refresh to get latest update!" in out[0].name


# ── the commands ──────────────────────────────────────────────────────────────

async def test_draw_posts_one_image_to_matchup_and_no_text(env):
    guild = FakeGuild()
    await connect(env, guild)
    i = interaction(member("CAP"), guild, channel=Chan("somewhere-else"))
    await env.cog.draw.callback(env.cog, i, 1)
    matchup = guild.channels_by["draw"]
    assert len(matchup.sent) == 1
    msg = matchup.sent[0]
    assert msg["content"] is None and len(msg["files"]) == 1 and msg["files"][0].filename.endswith(".png")
    assert "#🎓︱matchup" in last_text(i)
    assert not i.channel.sent


async def test_each_command_goes_to_its_own_channel(env):
    guild = FakeGuild()
    await connect(env, guild)
    cog = env.cog
    plan = [("result", "result", (1,)), ("break_", "break", ("Open",))]
    for method, kind, args in plan:
        i = interaction(member("ORG"), guild)
        await getattr(cog, method).callback(cog, i, *args)
        chan = guild.channels_by[kind]
        assert len(chan.sent) >= 1 and chan.sent[-1]["content"] is None and chan.sent[-1]["files"], kind


def sent_view(i):
    return i.followup.send.await_args.kwargs.get("view")


async def press(env, guild, view, button, who="CAP"):
    i = interaction(member(who), guild)                               # (each fake interaction brings its own channels)
    await button.callback(i)
    return i, guild.channels_by["motion"]


async def test_motion_with_info_slide_shows_both_buttons_and_posts_nothing(env):
    guild = FakeGuild()
    await connect(env, guild)
    i = interaction(member("CAP"), guild)
    await env.cog.motion.callback(env.cog, i, 1)                      # the fixture's motion has an info slide
    view = sent_view(i)
    assert isinstance(view, cog_mod.MotionRevealView)
    assert [b.label for b in view.children] == ["Reveal Info Slide", "Reveal Motion"]
    assert view.reveal_motion.disabled and not view.reveal_info.disabled     # info slide first, then the motion
    assert not guild.channels_by["motion"].sent                              # choosing the round posts nothing


async def test_motion_without_info_slide_shows_one_button(env):
    guild = FakeGuild()
    await connect(env, guild)
    for rnd in env.cog._cache[guild.id].rounds:
        for m in rnd.motions:
            m.info_slide = ""
    i = interaction(member("CAP"), guild)
    await env.cog.motion.callback(env.cog, i, 1)
    view = sent_view(i)
    assert [b.label for b in view.children] == ["Reveal Motion"] and not view.children[0].disabled
    assert not guild.channels_by["motion"].sent
    _, chan = await press(env, guild, view, view.reveal_motion)
    assert len(chan.sent) == 1 and chan.sent[0]["files"][0].filename.startswith("motion-")


async def test_reveal_buttons_post_info_then_motion(env):
    guild = FakeGuild()
    await connect(env, guild)
    i = interaction(member("CAP"), guild)
    await env.cog.motion.callback(env.cog, i, 1)
    view = sent_view(i)

    p1, chan = await press(env, guild, view, view.reveal_info)
    assert len(chan.sent) == 1 and chan.sent[0]["files"][0].filename.startswith("info-")
    assert view.reveal_info.disabled and not view.reveal_motion.disabled     # motion unlocked, info greyed out
    p1.response.edit_message.assert_awaited()

    _, chan = await press(env, guild, view, view.reveal_motion)
    assert len(chan.sent) == 1 and chan.sent[0]["files"][0].filename.startswith("motion-")
    assert view.reveal_motion.disabled


async def test_reveal_buttons_are_for_operators_only(env):
    guild = FakeGuild()
    await connect(env, guild)
    view = cog_mod.MotionRevealView(env.cog, env.cog._cache[guild.id].rounds[0], "T", True)
    bad = interaction(member("DEBATER"), guild)
    assert await view.interaction_check(bad) is False and "Only CAP" in last_text(bad)
    assert await view.interaction_check(interaction(member("ORG"), guild)) is True


def multi_round(infos):
    from tabbycat_client import MotionInfo, RoundInfo
    return RoundInfo(seq=1, name="Round 1", draw_status="R", motions_released=True,
                     motions=[MotionInfo(i, f"Motion text {i}", info) for i, info in enumerate(infos, 1)])


async def test_several_motions_get_one_button_each_plus_all_buttons(env):
    view = cog_mod.MotionRevealView(env.cog, multi_round(["info 1", "", "info 3"]), "T")
    info = [b.label for b in view.info_buttons.values()]
    motion = [b.label for b in view.motion_buttons.values()]
    assert info == ["Info 1", "Info 3", "All info slides"]             # only motions that have an info slide
    assert motion == ["Motion 1", "Motion 2", "Motion 3", "All motions"]
    locked = {b.label: b.disabled for b in view.motion_buttons.values()}
    assert locked == {"Motion 1": True, "Motion 2": False, "Motion 3": True, "All motions": True}
    assert all(b.row <= 4 for b in view.children)


async def test_several_motions_reveal_individually_then_all(env):
    guild = FakeGuild()
    await connect(env, guild)
    view = cog_mod.MotionRevealView(env.cog, multi_round(["info 1", "", "info 3"]), "T")

    _, chan = await press(env, guild, view, view.info_buttons[1])
    assert [f.filename for f in chan.sent[0]["files"]] == ["info-round-1-1.png"]
    assert not view.motion_buttons[1].disabled and view.motion_buttons[3].disabled       # Motion 1 unlocked, 3 still locked
    assert view.motion_buttons[None].disabled                                           # "all" waits for every info slide

    _, chan = await press(env, guild, view, view.motion_buttons[2])                      # no info slide → available at once
    assert [f.filename for f in chan.sent[0]["files"]] == ["motion-round-1-2.png"]

    _, chan = await press(env, guild, view, view.info_buttons[None])                     # "all info" = what is still missing
    assert [f.filename for f in chan.sent[0]["files"]] == ["info-round-1-3.png"]
    assert view.info_buttons[None].disabled and view.info_buttons[3].disabled
    assert not view.motion_buttons[None].disabled

    _, chan = await press(env, guild, view, view.motion_buttons[None])                   # remaining motions 1 and 3
    assert [f.filename for f in chan.sent[0]["files"]] == ["motion-round-1-1.png", "motion-round-1-3.png"]
    assert all(b.disabled for b in view.children)


async def test_release_all_from_the_start_when_nothing_is_locked(env):
    guild = FakeGuild()
    await connect(env, guild)
    view = cog_mod.MotionRevealView(env.cog, multi_round(["", "", ""]), "T")
    assert [b.label for b in view.children] == ["Motion 1", "Motion 2", "Motion 3", "All motions"]
    _, chan = await press(env, guild, view, view.motion_buttons[None])
    assert len(chan.sent[0]["files"]) == 3


async def test_failed_reveal_unlocks_the_button_again(env, monkeypatch):
    guild = FakeGuild()
    await connect(env, guild)
    view = cog_mod.MotionRevealView(env.cog, multi_round(["info 1", "info 2"]), "T")

    def boom(*a, **k):
        raise RuntimeError("render failed")
    monkeypatch.setattr(cog_mod.tab_render, "render_info_slides", boom)
    i, _ = await press(env, guild, view, view.info_buttons[None])
    assert not view.info_buttons[None].disabled and not view.info_buttons[1].disabled
    assert "❌" in last_text(i)


async def test_very_many_motions_still_fit_discords_button_limit(env):
    view = cog_mod.MotionRevealView(env.cog, multi_round(["i"] * 12), "T")
    assert [b.label for b in view.children] == ["All info slides", "All motions"]


async def test_unavailable_choice_tells_the_user_to_refresh(env):
    guild = FakeGuild()
    await connect(env, guild)
    for value in (2, 99, cog_mod.NONE_VALUE):                          # draft round, unknown round, placeholder option
        i = interaction(member("TABBY"), guild)
        await env.cog.draw.callback(env.cog, i, value)
        assert "/refresh" in last_text(i)
    assert not guild.channels_by["draw"].sent


async def test_not_connected_message(env):
    i = interaction(member("TABBY"))
    await env.cog.draw.callback(env.cog, i, 1)
    assert "/tab" in last_text(i)


async def test_refresh_reports_availability(env):
    guild = FakeGuild()
    await connect(env, guild)
    i = interaction(member("TABBY"), guild)
    await env.cog.refresh.callback(env.cog, i)
    desc = i.followup.send.await_args.kwargs["embed"].description
    assert "Draw:" in desc and "Round 1, Round 3" in desc and "Break:** Open" in desc


async def test_tabbycat_outage_uses_cached_data_or_reports(env):
    guild = FakeGuild()
    await connect(env, guild)
    await env.srv.close()                                              # the Tabbycat site goes away
    env.cog._cache[guild.id].fetched -= cog_mod.CACHE_TTL + 1         # cache is stale
    i = interaction(member("CAP"), guild)
    await env.cog.motion.callback(env.cog, i, 1)                       # motion needs no live request: served from cache
    view = sent_view(i)
    assert view is not None                                            # buttons are served from the cache
    await view.reveal_info.callback(interaction(member("CAP"), guild))
    assert guild.channels_by["motion"].sent
    i2 = interaction(member("CAP"), guild)
    await env.cog.draw.callback(env.cog, i2, 1)                        # draw needs the site -> friendly error
    assert "❌" in last_text(i2) or "Could not" in last_text(i2)

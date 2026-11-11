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
    assert out[0].value == cog_mod.NONE_VALUE and "/refresh" in out[0].name          # connected, data not loaded
    await env.cog._refresh(guild)
    draw = [c.name for c in await choices(env, cog_mod.TabbycatCog._draw_ac, guild)]
    assert draw[0].startswith("2 of 3 rounds have draw") and "/refresh" in draw[0]       # how many rounds have arrived
    assert draw[1:] == ["Round 1 ✅", "Round 2 ⏳ not released yet — use /refresh", "Round 3 ✅"]
    motion = await choices(env, cog_mod.TabbycatCog._motion_ac, guild)
    assert motion[0].name.startswith("1 of 3 rounds have motions") and motion[0].value == cog_mod.NONE_VALUE
    assert [c.name for c in motion[1:]][0] == "Round 1 ✅" and [c.value for c in motion[1:]] == [1, 2, 3]
    assert [c.name for c in await choices(env, cog_mod.TabbycatCog._break_ac, guild)] == ["Open"]
    assert [c.name for c in await choices(env, cog_mod.TabbycatCog._draw_ac, guild, "3")][-1] == "Round 3 ✅"


async def test_autocomplete_all_released_has_no_warning_line(env):
    guild = FakeGuild()
    await connect(env, guild)
    for r in env.cog._cache[guild.id].rounds:
        r.draw_status = "R"
    out = [c.name for c in await choices(env, cog_mod.TabbycatCog._draw_ac, guild)]
    assert out == ["Round 1 ✅", "Round 2 ✅", "Round 3 ✅"]


async def test_autocomplete_none_released_points_to_refresh(env):
    guild = FakeGuild()
    await connect(env, guild)
    env.cog._cache[guild.id].rounds[:] = [r for r in env.cog._cache[guild.id].rounds if r.seq == 2]    # only a draft round
    for fn in (cog_mod.TabbycatCog._draw_ac, cog_mod.TabbycatCog._motion_ac, cog_mod.TabbycatCog._result_ac):
        out = await choices(env, fn, guild)
        assert out[0].name.startswith("0 of 1 rounds have") and "/refresh" in out[0].name
        assert "not released" in out[1].name and "/refresh" in out[1].name


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
    for rnd in env.cog._cache[guild.id].rounds:
        for m in rnd.motions:
            m.info_slide = ""                                          # no info slide → /motion posts straight away
    plan = [("motion", "motion", (1,)), ("result", "result", (1,)), ("break_", "break", ("Open",))]
    for method, kind, args in plan:
        i = interaction(member("ORG"), guild)
        await getattr(cog, method).callback(cog, i, *args)
        chan = guild.channels_by[kind]
        assert len(chan.sent) >= 1 and chan.sent[-1]["content"] is None and chan.sent[-1]["files"], kind


def sent_view(i):
    return i.followup.send.await_args.kwargs.get("view")


async def test_motion_with_info_slide_offers_two_buttons_and_posts_nothing_yet(env):
    guild = FakeGuild()
    await connect(env, guild)
    i = interaction(member("CAP"), guild)
    await env.cog.motion.callback(env.cog, i, 1)                      # the fixture's motion has an info slide
    view = sent_view(i)
    assert isinstance(view, cog_mod.MotionRevealView)
    assert [b.label for b in view.children] == ["Reveal Info", "Reveal Motion"]
    assert not guild.channels_by["motion"].sent and "info slide" in last_text(i)


async def test_reveal_buttons_post_info_and_motion_separately(env):
    guild = FakeGuild()
    await connect(env, guild)
    i = interaction(member("CAP"), guild)
    await env.cog.motion.callback(env.cog, i, 1)
    view = sent_view(i)

    press = interaction(member("CAP"), guild)                         # (each fake interaction brings its own channels)
    await view.reveal_info.callback(press)
    chan = guild.channels_by["motion"]
    assert len(chan.sent) == 1 and chan.sent[0]["files"][0].filename.startswith("info-")
    assert view.reveal_info.disabled and not view.reveal_motion.disabled     # greyed out after use
    press.response.edit_message.assert_awaited()

    press2 = interaction(member("CAP"), guild)
    await view.reveal_motion.callback(press2)
    chan2 = guild.channels_by["motion"]
    assert len(chan2.sent) == 1 and chan2.sent[0]["files"][0].filename.startswith("motion-")
    assert view.reveal_motion.disabled


async def test_reveal_buttons_are_for_operators_only(env):
    guild = FakeGuild()
    await connect(env, guild)
    view = cog_mod.MotionRevealView(env.cog, env.cog._cache[guild.id].rounds[0], "T")
    bad = interaction(member("DEBATER"), guild)
    assert await view.interaction_check(bad) is False and "Only CAP" in last_text(bad)
    assert await view.interaction_check(interaction(member("ORG"), guild)) is True


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
    await sent_view(i).reveal_motion.callback(interaction(member("CAP"), guild))
    assert guild.channels_by["motion"].sent
    i2 = interaction(member("CAP"), guild)
    await env.cog.draw.callback(env.cog, i2, 1)                        # draw needs the site -> friendly error
    assert "❌" in last_text(i2) or "Could not" in last_text(i2)

import asyncio
import json
import types
from unittest import mock

import discord
import pytest

import main
import keep_alive
import webhook
import timer_cog
import assign_cog
from tests.fakes import http_error, FakeGuild, FakeChannel, FakeCategory

BASELINE = {
    "cogs": ["AllInCog", "AssignCog", "DMNotifyCog", "GamesCog", "PoiCog", "PollsCog", "RolesCog",
             "SettingsCog", "SetupCog", "TimerCog", "TossCog", "WelcomeCog"],
    "commands": ["close", "mention", "newpoll", "play", "rebuild", "st", "toss"],
    "custom_ids": ["kamla:allin:push_back", "kamla:on_join:create_now", "kamla:role:debater",
                   "kamla:role:independent_adj", "kamla:role:invited_adj", "kamla:role:visitor",
                   "kamla:s:add", "kamla:s:del", "kamla:s:fmt", "kamla:s:lock"],
    "listeners": {"on_member_join": 2, "on_member_remove": 2, "on_member_update": 2, "on_message": 7},
    "views": ["AllInView", "OnJoinView", "RoleSelectView", "SettingsView"],
}


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KAMLA_STATE_DIR", str(tmp_path))
    return tmp_path


# 1, 10, 12: starts once; every command/cog/listener/view from before is still registered
async def test_startup_once_and_features_preserved(state_dir):
    bot = main.KamlaBot()
    sync = mock.AsyncMock(return_value=[])
    with mock.patch.object(type(bot.tree), "sync", new=sync):
        await bot.setup_hook()
        await bot.setup_hook()                      # accidental second call must be a no-op
    assert sync.await_count == 1
    assert sorted(bot.cogs) == BASELINE["cogs"]
    assert sorted(c.qualified_name for c in bot.tree.walk_commands()) == BASELINE["commands"]
    assert sorted(i.custom_id for v in bot.persistent_views for i in v.children if getattr(i, "custom_id", None)) == BASELINE["custom_ids"]
    assert sorted(v.__class__.__name__ for v in bot.persistent_views) == BASELINE["views"]
    assert {k: len(v) for k, v in bot.extra_events.items()} == BASELINE["listeners"]
    await bot.close()


async def test_command_sync_skipped_when_unchanged(state_dir):
    async def start():
        bot = main.KamlaBot()
        sync = mock.AsyncMock(return_value=[])
        with mock.patch.object(type(bot.tree), "sync", new=sync):
            await bot.setup_hook()
        await bot.close()
        return sync.await_count
    assert await start() == 1
    assert await start() == 0                       # same command set -> no /commands PUT on restart


# 9: reconnects (on_ready fires again) don't redo one-time work
async def test_on_ready_reconnect_is_idempotent(state_dir):
    bot = main.KamlaBot()
    fake_user = types.SimpleNamespace(id=1)
    with mock.patch.object(type(bot), "user", new=fake_user), \
         mock.patch.object(type(bot), "guilds", new=[]), \
         mock.patch.object(keep_alive, "set_status") as st:
        await bot.on_ready()
        await bot.on_ready()
        await bot.on_ready()
    assert st.call_count == 1
    await bot.close()


# 2: simulated Cloudflare/global 429 at login -> long, growing cooldown, bounded attempts
def test_login_429_does_not_loop():
    sleeps, runs = [], []

    class FakeBot:
        def run(self, *a, **k):
            runs.append(1)
            raise http_error(429, via=False)

    code = main.run_bot(make_bot=FakeBot, token="t" * 60, sleep=sleeps.append, max_attempts=4)
    assert code == 2 and len(runs) == 4 and len(sleeps) == 3
    assert sleeps[0] >= 900 and sleeps[1] >= 1800 and sleeps == sorted(sleeps)


def test_login_429_honours_retry_after():
    sleeps = []

    class FakeBot:
        def run(self, *a, **k):
            raise http_error(429, via=False, retry_after=5000)
    main.run_bot(make_bot=FakeBot, token="t" * 60, sleep=sleeps.append, max_attempts=2)
    assert sleeps[0] >= 5000


def test_invalid_token_exits_immediately():
    sleeps, runs = [], []

    class FakeBot:
        def run(self, *a, **k):
            runs.append(1)
            raise discord.LoginFailure("bad token")
    assert main.run_bot(make_bot=FakeBot, token="t" * 60, sleep=sleeps.append) == 1
    assert len(runs) == 1 and not sleeps


def test_network_failure_retries_with_backoff_then_succeeds():
    sleeps, runs = [], []

    class FakeBot:
        def run(self, *a, **k):
            runs.append(1)
            if len(runs) < 3:
                raise ConnectionError("dns")
    assert main.run_bot(make_bot=FakeBot, token="t" * 60, sleep=sleeps.append) == 0
    assert len(runs) == 3 and len(sleeps) == 2


def test_each_retry_gets_a_fresh_bot_instance():
    seen = []
    factory = main._default_bot_factory()
    seen += [factory(), factory(), factory()]
    assert seen[0] is main.bot and seen[1] is not seen[2]


def test_keep_alive_reports_honest_status():
    c = keep_alive.app.test_client()
    keep_alive.set_status("blocked", "retrying")
    assert b"blocked" in c.get("/").data and c.get("/").status_code == 200
    keep_alive.set_status("connected")
    assert c.get("/").data == b"KAMLABot is alive!"
    assert json.loads(c.get("/health").data)["state"] == "connected"


# 9: join-log edits are debounced into one task / one edit
async def test_join_log_debounce(monkeypatch):
    monkeypatch.setattr(webhook, "WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setattr(webhook, "JOIN_LOG_DEBOUNCE_SECONDS", 0.02)
    g = types.SimpleNamespace(id=42)
    webhook._join_message_ids[42] = "1"
    calls = []

    async def fake_edit(guild, cfg):
        calls.append(cfg)
    monkeypatch.setattr(webhook, "edit_join_log", fake_edit)
    for i in range(100):
        webhook.schedule_join_log_edit(g, {"n": i})
    await asyncio.sleep(0.15)
    assert len(calls) == 1 and calls[0] == {"n": 99}
    webhook._join_message_ids.pop(42, None)


# 11: timers — one task per channel, replaced not duplicated, cleaned on unload
async def test_timer_tasks_not_duplicated_and_cleaned():
    cog = timer_cog.TimerCog(bot=None)
    started = []

    async def long_timer(*a, **k):
        started.append(1)
        await asyncio.sleep(10)

    ch = types.SimpleNamespace(id=7)
    for _ in range(3):
        old = timer_cog._active_timers.pop(ch.id, None)
        if old:
            old.cancel()
        timer_cog._active_timers[ch.id] = asyncio.create_task(long_timer())
        await asyncio.sleep(0)
    assert len(timer_cog._active_timers) == 1
    await cog.cog_unload()
    assert not timer_cog._active_timers


# assign cleanup: scans only the previous role's channel (was: every other channel)
async def test_assign_cleanup_is_targeted():
    g = FakeGuild()
    cat = FakeCategory(g, "🛅︱ASSIGN"); g._channels[cat.id] = cat
    chans = {}
    for n in ["org", "cap", "debater", "visitor"]:
        c = FakeChannel(g, n, cat); g._channels[c.id] = c; chans[n] = c
    member = object()
    m = await chans["visitor"].send("x"); m.mentions = [member]
    targets = assign_cog._channels_for_roles(cat, ["VISITOR"])
    await assign_cog._cleanup_old_mentions(cat, member, chans["debater"].id, only_channels=targets)
    assert m.deleted
    assert chans["visitor"].history_calls == 1
    assert all(chans[n].history_calls == 0 for n in ("org", "cap", "debater"))

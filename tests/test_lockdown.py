import asyncio
import types

import pytest

import lockdown
from ratelimit import RequestCoordinator
from tests.fakes import FakeGuild


class MemConfig:
    """config_manager stand-in that remembers what the lock saved."""
    def __init__(self):
        self.data = {}

    async def get_config(self, guild):
        return dict(self.data.get(guild.id, {}))

    async def set_config(self, guild, d):
        self.data[guild.id] = dict(d)
        return True


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(lockdown, "config_manager", MemConfig())
    lockdown._owned.clear()
    async def nosleep(_):
        return None
    c = RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep})
    monkeypatch.setattr(lockdown, "bulk", c)
    monkeypatch.setattr(lockdown, "notify", c)
    monkeypatch.setattr(lockdown, "NOTIFY_BATCH_SECONDS", 0.05)
    lockdown._pending.clear()


class Chan:
    _next = iter(range(5000, 6000))

    def __init__(self, name, delay=0, voice=False):
        self.id = next(Chan._next)
        self.name, self.slowmode_delay, self.voice = name, delay, voice
        self.edits = 0

    async def edit(self, slowmode_delay=None, reason=None):
        self.slowmode_delay = slowmode_delay
        self.edits += 1


def guild_with(*chans, members=()):
    g = types.SimpleNamespace(id=1, name="G", owner_id=99, text_channels=[c for c in chans if not c.voice],
                              voice_channels=[c for c in chans if c.voice], members=list(members))
    g.get_member = lambda i: next((m for m in g.members if m.id == i), None)
    return g


async def test_lock_sets_5s_everywhere_and_unlock_removes_only_what_it_set():
    a, b, custom, keep = Chan("general"), Chan("🎓︱motion"), Chan("slow", delay=30), Chan("kamla-config")
    v = Chan("vc", voice=True)
    g = guild_with(a, b, custom, keep, v)
    await lockdown.start_slowmode(g, 5)
    assert (a.slowmode_delay, b.slowmode_delay, v.slowmode_delay) == (5, 5, 5)
    assert custom.slowmode_delay == 30 and keep.slowmode_delay == 0           # longer custom value and config channel untouched
    await lockdown.start_slowmode(g, 0)
    assert (a.slowmode_delay, b.slowmode_delay, v.slowmode_delay) == (0, 0, 0)
    assert custom.slowmode_delay == 30                                        # not ours to reset


async def test_second_lock_is_a_noop_and_quick_toggle_supersedes():
    a = Chan("a")
    g = guild_with(a)
    await lockdown.start_slowmode(g, 5)
    await lockdown.start_slowmode(g, 5)
    assert a.edits == 1
    t1 = lockdown.start_slowmode(g, 0)
    t2 = lockdown.start_slowmode(g, 5)
    await asyncio.gather(t1, t2, return_exceptions=True)
    assert a.slowmode_delay == 5


class Org:
    def __init__(self, uid, roles=("ORG",)):
        self.id, self.bot = uid, False
        self.roles = [types.SimpleNamespace(name=r) for r in roles]
        self.dms = []

    async def send(self, embed=None, **kw):
        self.dms.append(embed)


async def test_blocked_channels_notify_every_org_member_once_per_burst():
    o1, o2, deb = Org(1), Org(2), Org(3, roles=("DEBATER",))
    g = guild_with(members=[o1, o2, deb])
    creator = types.SimpleNamespace(mention="<@7>", name="eve")
    for name in ("spam-1", "spam-2", "spam-3"):
        lockdown.report_blocked_channel(g, creator, name)
    await asyncio.sleep(0.3)
    assert len(o1.dms) == 1 and len(o2.dms) == 1 and not deb.dms            # one DM each, debaters not told
    text = o1.dms[0].description
    assert "<@7>" in text and all(f"#spam-{i}" in text for i in (1, 2, 3))


async def test_unknown_creator_and_owner_fallback():
    owner = Org(99, roles=("DEBATER",))
    g = guild_with(members=[owner])                                           # no ORG members at all
    lockdown.report_blocked_channel(g, None, "x")
    await asyncio.sleep(0.2)
    assert len(owner.dms) == 1 and "audit log was unavailable" in owner.dms[0].description


async def test_closed_dms_do_not_crash():
    class Closed(Org):
        async def send(self, **kw):
            from tests.fakes import http_error
            raise http_error(403)
    g = guild_with(members=[Closed(1)])
    lockdown.report_blocked_channel(g, None, "x")
    await asyncio.sleep(0.2)                                                  # must simply not raise


# ── integration with main.on_guild_channel_create ─────────────────────────────

async def _handler_setup(monkeypatch, creator_id, locked=True):
    import main
    import discord
    from unittest import mock
    bot = main.KamlaBot()
    monkeypatch.setattr(type(bot), "user", types.SimpleNamespace(id=555), raising=False)
    main.config_manager._cache[42] = {"locked": locked}

    class Entry:
        def __init__(self, user, target):
            self.user, self.target = user, target

    calls = {"delete": 0, "report": [], "slow": []}

    class NewChannel:
        id, name = 900, "sneaky"
        async def delete(self, reason=None):
            calls["delete"] += 1

    class G:
        id, name, owner_id, members = 42, "G", 99, []
        text_channels = []
        me = types.SimpleNamespace(id=555)

        def audit_logs(self, action=None, limit=10):
            async def gen():
                yield Entry(types.SimpleNamespace(id=creator_id, mention=f"<@{creator_id}>", name="u"), NewChannel)
            return gen()

    import lockdown as ld
    monkeypatch.setattr(ld, "report_blocked_channel", lambda g, c, n: calls["report"].append((c.id if c else None, n)))

    async def fake_apply(ch, s, owned=None):
        calls["slow"].append(s)
        return True

    async def fake_save(guild):
        calls.setdefault("saved", []).append(guild.id)
    monkeypatch.setattr(ld, "apply_to_channel", fake_apply)
    monkeypatch.setattr(ld, "save_owned", fake_save)
    ch = NewChannel()
    ch.guild = G()
    return bot, ch, calls


async def test_locked_server_deletes_new_channel_and_notifies(monkeypatch):
    bot, ch, calls = await _handler_setup(monkeypatch, creator_id=7)
    await bot.on_guild_channel_create(ch)
    assert calls["delete"] == 1 and calls["report"] == [(7, "sneaky")]
    await bot.close()


async def test_locked_server_allows_kamla_but_applies_slowmode(monkeypatch):
    bot, ch, calls = await _handler_setup(monkeypatch, creator_id=555)
    await bot.on_guild_channel_create(ch)
    assert calls["delete"] == 0 and calls["report"] == [] and calls["slow"] == [5]
    assert calls["saved"] == [42]                                       # the lock's channel list is saved too
    await bot.close()


async def test_unlocked_server_ignores_new_channels(monkeypatch):
    bot, ch, calls = await _handler_setup(monkeypatch, creator_id=7, locked=False)
    await bot.on_guild_channel_create(ch)
    assert calls["delete"] == 0 and calls["report"] == []
    await bot.close()


# ── the lock only undoes slowmode it set itself ───────────────────────────────

async def test_channel_that_already_had_5s_keeps_it_after_unlock():
    mine, theirs = Chan("general"), Chan("announcements", delay=5)      # "theirs" had a 5 s slowmode before the lock
    g = guild_with(mine, theirs)
    await lockdown.start_slowmode(g, 5)
    assert mine.slowmode_delay == 5 and theirs.slowmode_delay == 5 and theirs.edits == 0
    await lockdown.start_slowmode(g, 0)
    assert mine.slowmode_delay == 0
    assert theirs.slowmode_delay == 5 and theirs.edits == 0             # not ours → left alone


async def test_lock_remembers_its_channels_across_a_restart():
    mine, theirs = Chan("general"), Chan("announcements", delay=5)
    g = guild_with(mine, theirs)
    await lockdown.start_slowmode(g, 5)
    assert lockdown.config_manager.data[g.id]["lock_slowmode"] == [mine.id]
    lockdown._owned.clear()                                             # KAMLA restarts: memory is gone, the config stays
    await lockdown.start_slowmode(g, 0)
    assert mine.slowmode_delay == 0 and theirs.slowmode_delay == 5
    assert "lock_slowmode" not in lockdown.config_manager.data[g.id]    # nothing left to remember


async def test_lock_made_by_an_older_kamla_still_unlocks():
    a, b = Chan("a", delay=5), Chan("b", delay=5)                       # no saved list: fall back to "exactly 5 s was ours"
    g = guild_with(a, b)
    await lockdown.start_slowmode(g, 0)
    assert (a.slowmode_delay, b.slowmode_delay) == (0, 0)


async def test_channel_changed_by_a_human_during_the_lock_is_not_reset():
    a = Chan("a")
    g = guild_with(a)
    await lockdown.start_slowmode(g, 5)
    a.slowmode_delay = 30                                               # a moderator picked their own value
    await lockdown.start_slowmode(g, 0)
    assert a.slowmode_delay == 30

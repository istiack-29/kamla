import asyncio
import types
from unittest import mock

import pytest

import nickname_cog as nc
from ratelimit import RequestCoordinator
from tests.fakes import http_error


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    async def nosleep(_):
        return None
    monkeypatch.setattr(nc, "bulk", RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep}))
    monkeypatch.setattr(nc, "DEBOUNCE_SECONDS", 0.02)


def make_guild(owner_id=1000, bot_pos=50):
    return types.SimpleNamespace(id=555, owner_id=owner_id, me=types.SimpleNamespace(top_role=types.SimpleNamespace(position=bot_pos)),
                                 members=[], get_member=lambda i: None)


def make_member(guild, uid, roles=(), nick=None, name="alice", global_name=None, pos=1, bot=False):
    m = types.SimpleNamespace(
        id=uid, guild=guild, bot=bot, nick=nick, name=name, global_name=global_name,
        roles=[types.SimpleNamespace(name=r) for r in roles],
        top_role=types.SimpleNamespace(position=pos),
    )
    m.edits = []

    async def edit(nick=None, reason=None):
        m.edits.append(nick)
        m.nick = nick
    m.edit = edit
    guild.members.append(m)
    return m


@pytest.mark.parametrize("role,tag", [
    ("ORG", "[O]"), ("EQUITY", "[E]"), ("TABBY", "[T]"), ("CAP", "[CA]"),
    ("INVITED ADJUDICATOR", "[J]"), ("INDEPENDENT ADJUDICATOR", "[J]"),
    ("VISITOR", "[V]"), ("DEBATER", "[D]"),
])
def test_each_role_gets_its_tag(role, tag):
    g = make_guild()
    m = make_member(g, 1, [role, "@everyone"], global_name="Alice")
    assert nc.desired_nick(m) == f"{tag} Alice"


def test_tag_replaces_old_tag_and_keeps_custom_nickname():
    g = make_guild()
    m = make_member(g, 1, ["DEBATER"], nick="[V] Rafi")
    assert nc.desired_nick(m) == "[D] Rafi"


def test_most_senior_role_wins():
    g = make_guild()
    assert nc.desired_nick(make_member(g, 1, ["DEBATER", "ORG"], name="x")) == "[O] x"


def test_length_never_exceeds_32():
    g = make_guild()
    m = make_member(g, 1, ["INDEPENDENT ADJUDICATOR"], name="x" * 60)
    assert len(nc.desired_nick(m)) == 32 and nc.desired_nick(m).startswith("[J] ")


def test_losing_all_roles_removes_the_tag():
    g = make_guild()
    assert nc.desired_nick(make_member(g, 1, [], nick="[D] Alice", name="alice", global_name="Alice")) is None
    assert nc.desired_nick(make_member(g, 2, [], nick="[D] Custom", name="alice", global_name="Alice")) == "Custom"
    assert nc.desired_nick(make_member(g, 3, [], nick="Untouched", name="a")) == "Untouched"   # not ours


async def test_sync_is_idempotent_and_skips_forbidden_members_without_requests():
    g = make_guild(owner_id=7, bot_pos=10)
    a = make_member(g, 1, ["DEBATER"], name="a")
    owner = make_member(g, 7, ["ORG"], name="o")
    boss = make_member(g, 8, ["CAP"], name="b", pos=99)       # above the bot
    robot = make_member(g, 9, ["DEBATER"], name="r", bot=True)
    assert await nc.sync_member(a) == "updated" and a.nick == "[D] a"
    assert await nc.sync_member(a) == "ok" and a.edits == ["[D] a"]       # second run: no request
    assert await nc.sync_member(owner) == "skipped" and owner.edits == []
    assert await nc.sync_member(boss) == "skipped" and boss.edits == []
    assert await nc.sync_member(robot) == "skipped" and robot.edits == []


async def test_forbidden_and_failures_are_reported_not_retried():
    g = make_guild()
    m = make_member(g, 1, ["DEBATER"], name="a")
    calls = []

    async def boom(nick=None, reason=None):
        calls.append(1)
        raise http_error(403)
    m.edit = boom
    assert await nc.sync_member(m) == "skipped" and len(calls) == 1


async def test_role_swap_causes_one_edit(monkeypatch):
    g = make_guild()
    m = make_member(g, 1, ["VISITOR"], nick="[V] a", name="a")
    g.get_member = lambda i: m
    cog = nc.NicknameCog(bot=None)
    # KAMLA removes the old role then adds the new one => two member-update events
    for roles in ([], ["DEBATER"]):
        m.roles = [types.SimpleNamespace(name=r) for r in roles]
        cog._schedule(m)
    await asyncio.sleep(0.3)
    assert m.edits == ["[D] a"]                      # the intermediate "no role" state never hit the API
    await cog.cog_unload()


async def test_command_syncs_everyone_and_reports():
    g = make_guild(owner_id=7)
    a = make_member(g, 1, ["DEBATER"], name="a")
    b = make_member(g, 2, ["ORG"], name="b")
    make_member(g, 3, ["VISITOR"], nick="[V] c", name="c")      # already correct
    owner = make_member(g, 7, ["ORG"], name="o")
    admin = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(administrator=True), roles=[])
    inter = mock.MagicMock(guild=g, user=admin)
    inter.response.defer = mock.AsyncMock()
    inter.edit_original_response = mock.AsyncMock()
    cog = nc.NicknameCog(bot=None)
    await cog.nicknames.callback(cog, inter)
    msg = inter.edit_original_response.await_args.kwargs["content"]
    assert "2** updated" in msg and "1** already correct" in msg and "1** skipped" in msg
    assert (a.nick, b.nick) == ("[D] a", "[O] b") and owner.edits == []
    # non-admin is refused
    stranger = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(administrator=False), roles=[])
    inter2 = mock.MagicMock(guild=g, user=stranger)
    inter2.response.send_message = mock.AsyncMock()
    await cog.nicknames.callback(cog, inter2)
    inter2.response.send_message.assert_awaited()

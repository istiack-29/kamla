"""Fallback tracking of role links that Discord would not create with a role (invite_roles_cog)."""
import asyncio
import types

import pytest

import invite_roles_cog as ir
import links_cog as lk
from ratelimit import RequestCoordinator
from tests.fakes import FakeConfig, http_error
from tests.test_links import LinkGuild


@pytest.fixture(autouse=True)
def env(monkeypatch):
    async def nosleep(_):
        return None
    fast = RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep, "rng": lambda: 0.0})
    cfg = FakeConfig()
    import config as cfgmod
    monkeypatch.setattr(cfgmod, "notify", fast)
    for mod in (ir, lk):
        monkeypatch.setattr(mod, "bulk", fast)
        monkeypatch.setattr(mod, "config_manager", cfg)
    monkeypatch.setattr(ir, "BATCH_DELAY", 0.02)

    async def refuse(channel, payload, reason):                       # force the plain-invite fallback
        raise http_error(400, code=50035)
    monkeypatch.setattr(lk, "_http_create_invite", refuse)
    ir._uses.clear(); ir._tracked.clear()


def member(guild, uid=1, bot=False):
    m = types.SimpleNamespace(id=uid, guild=guild, bot=bot, granted=[], roles=[])

    async def add_roles(*roles, reason=None):
        m.granted += [r.name for r in roles]
        m.roles += list(roles)

    async def remove_roles(*roles, reason=None):
        m.roles = [r for r in m.roles if r not in roles]
    m.add_roles, m.remove_roles = add_roles, remove_roles
    return m


async def joined_via(cog, g, code, who):
    g.live[code].uses += 1
    for m in who:
        await cog.on_member_join(m)
    await asyncio.sleep(0.07)


async def setup_guild():
    g = LinkGuild()
    rows = await lk.refresh_links(g)
    return g, {r["key"]: r for r in rows}


async def test_member_joining_via_each_fallback_link_gets_that_role():
    g, rows = await setup_guild()
    cog = ir.InviteRolesCog(bot=None)
    keys = ["org", "captain", "tabby", "equity", "invited", "independent", "debater", "visitor"]
    people = [member(g, i) for i in range(1, len(keys) + 1)]
    for key, who in zip(keys, people):
        await joined_via(cog, g, rows[key]["code"], [who])
    expected = [["ORG"], ["CAP"], ["TABBY"], ["EQUITY"], ["INVITED ADJUDICATOR"], ["INDEPENDENT ADJUDICATOR"],
                ["DEBATER"], ["VISITOR"]]
    assert [p.granted for p in people] == expected
    assert all(len(p.roles) == 1 for p in people)                    # never more than one role


async def test_the_kamla_link_never_tries_to_hand_out_the_bots_own_role():
    g, rows = await setup_guild()
    cog = ir.InviteRolesCog(bot=None)
    dev = member(g, 77)
    await joined_via(cog, g, rows["kamla"]["code"], [dev])
    assert dev.granted == [] and dev.roles == []                     # a managed role cannot be assigned


async def test_invite_never_stacks_roles_on_someone_who_already_has_one():
    g, rows = await setup_guild()
    cog = ir.InviteRolesCog(bot=None)
    rejoiner = member(g, 1)
    rejoiner.roles = [r for r in g.roles if r.name == "VISITOR"]
    await joined_via(cog, g, rows["org"]["code"], [rejoiner])
    assert [r.name for r in rejoiner.roles] == ["ORG"]


async def test_burst_of_joins_on_one_link_costs_one_lookup():
    g, rows = await setup_guild()
    cog = ir.InviteRolesCog(bot=None)
    ms = [member(g, i) for i in range(10, 20)]
    g.live[rows["visitor"]["code"]].uses += 10
    calls = getattr(g, "invite_calls", 0)
    counter = {"n": 0}
    orig = g.invites

    async def counting():
        counter["n"] += 1
        return await orig()
    g.invites = counting
    for m in ms:
        await cog.on_member_join(m)
    await asyncio.sleep(0.07)
    assert counter["n"] == 1 and all(m.granted == ["VISITOR"] for m in ms)


async def test_ambiguous_joins_never_grant_roles():
    g, rows = await setup_guild()
    cog = ir.InviteRolesCog(bot=None)
    a, b = member(g, 1), member(g, 2)
    g.live[rows["visitor"]["code"]].uses += 1
    g.live[rows["org"]["code"]].uses += 1                            # two invites changed, one an organiser link
    await cog.on_member_join(a); await cog.on_member_join(b)
    await asyncio.sleep(0.07)
    assert a.granted == [] and b.granted == []
    c = member(g, 3)
    g.live[rows["org"]["code"]].uses += 2                            # more uses than joins we saw
    await cog.on_member_join(c)
    await asyncio.sleep(0.07)
    assert c.granted == []


async def test_native_links_need_no_tracking_and_never_double_assign(monkeypatch):
    async def accept(channel, payload, reason):
        return {"code": channel.guild._new().code}
    monkeypatch.setattr(lk, "_http_create_invite", accept)
    g = LinkGuild()
    rows = {r["key"]: r for r in await lk.refresh_links(g)}
    cog = ir.InviteRolesCog(bot=None)
    m = member(g, 5)
    await joined_via(cog, g, rows["org"]["code"], [m])
    assert m.granted == []                                           # Discord grants native roles itself


async def test_normal_invite_and_untracked_guild_do_nothing():
    g, rows = await setup_guild()
    from tests.test_links import Inv
    g.live["plain"] = Inv("plain")
    ir._uses[g.id]["plain"] = 0
    cog = ir.InviteRolesCog(bot=None)
    m = member(g, 1)
    await joined_via(cog, g, "plain", [m])
    assert m.granted == []
    other = LinkGuild()
    await cog.on_member_join(member(other, 5))
    await asyncio.sleep(0.07)
    assert other.id not in ir._tracked


async def test_priming_only_touches_guilds_with_fallback_links():
    g1, g2 = LinkGuild(), LinkGuild()
    ir.config_manager.data[g1.id] = {"invite_roles": {"x": {"key": "visitor", "roles": ["VISITOR"]}}}
    counter = {"n": 0}
    for g in (g1, g2):
        orig = g.invites

        async def counting(orig=orig, g=g):
            counter[g.id] = counter.get(g.id, 0) + 1
            return await orig()
        g.invites = counting
    cog = ir.InviteRolesCog(bot=types.SimpleNamespace(guilds=[g1, g2]))
    await cog.on_ready(); await cog.on_ready()                       # reconnect must not re-prime
    await cog._prime_task
    assert counter.get(g1.id) == 1 and counter.get(g2.id) is None and g1.id in ir._tracked

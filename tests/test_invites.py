import asyncio
import types

import pytest

import invite_roles_cog as ir
from ratelimit import RequestCoordinator
from tests.fakes import FakeGuild, FakeChannel, FakeConfig


@pytest.fixture(autouse=True)
def env(monkeypatch):
    async def nosleep(_):
        return None
    fast = RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep})
    monkeypatch.setattr(ir, "bulk", fast)
    import config as cfgmod
    monkeypatch.setattr(cfgmod, "notify", fast)
    monkeypatch.setattr(ir, "BATCH_DELAY", 0.02)
    monkeypatch.setattr(ir, "config_manager", FakeConfig())
    ir._uses.clear()
    ir._tracked.clear()


class Inv:
    def __init__(self, code, uses=0):
        self.code, self.uses = code, uses
        self.url = f"https://discord.gg/{code}"
        self.guild = None


class InviteGuild(FakeGuild):
    def __init__(self):
        super().__init__()
        self.live: dict[str, Inv] = {}
        self.invite_calls = 0
        self.n = 0
        for name in ("VISITOR", "CAP", "TABBY", "ORG", "EQUITY"):
            self.roles.append(types.SimpleNamespace(name=name, id=hash(name)))

    async def invites(self):
        self.invite_calls += 1
        return list(self.live.values())

    def make_welcome(self):
        ch = FakeChannel(self, ir.WELCOME_CHANNEL)
        self._channels[ch.id] = ch

        async def create_invite(**kw):
            self.n += 1
            inv = Inv(f"code{self.n}")
            self.live[inv.code] = inv
            assert kw["max_age"] == 0 and kw["max_uses"] == 0 and kw["unique"] is True   # lifetime, own link
            return inv
        ch.create_invite = create_invite
        return ch


def member(guild, uid=1, bot=False):
    m = types.SimpleNamespace(id=uid, guild=guild, bot=bot, granted=[], roles=[])

    async def add_roles(*roles, reason=None):
        m.granted += [r.name for r in roles]
        m.roles += list(roles)

    async def remove_roles(*roles, reason=None):
        m.roles = [r for r in m.roles if r not in roles]
    m.add_roles, m.remove_roles = add_roles, remove_roles
    return m


async def test_creates_five_lifetime_invites_each_with_exactly_one_role():
    g = InviteGuild(); g.make_welcome()
    links = await ir.create_role_invites(g)
    assert [l["key"] for l in links] == ["visitor", "captain", "tabby", "org", "equity"]
    assert [l["roles"] for l in links] == [["VISITOR"], ["CAP"], ["TABBY"], ["ORG"], ["EQUITY"]]
    assert len({l["url"] for l in links}) == 5 and len(g.live) == 5


async def test_rerun_reuses_live_invites_and_recreates_dead_ones():
    g = InviteGuild(); g.make_welcome()
    first = await ir.create_role_invites(g)
    again = await ir.create_role_invites(g)
    assert [l["code"] for l in again] == [l["code"] for l in first] and len(g.live) == 5
    del g.live[first[1]["code"]]                                  # one expired/deleted
    third = await ir.create_role_invites(g)
    assert third[0]["code"] == first[0]["code"] and third[1]["code"] != first[1]["code"]


async def test_dm_text_contains_copyable_links_and_instruction():
    g = InviteGuild(); g.make_welcome()
    links = await ir.create_role_invites(g)
    text = ir.dm_text("My Cup", links)
    for l in links:
        assert f"<{l['url']}>" in text
    assert "send the links as per your need" in text.lower()
    assert "administrator" in text and "[T]" in text and "[O]" in text and "[E]" in text


async def joined_via(cog, g, code, who):
    g.live[code].uses += 1
    for m in who:
        await cog.on_member_join(m)
    await asyncio.sleep(0.2)


async def test_member_joining_via_each_link_gets_those_roles():
    g = InviteGuild(); g.make_welcome()
    links = await ir.create_role_invites(g)
    cog = ir.InviteRolesCog(bot=None)
    people = [member(g, i) for i in range(1, 6)]
    for link, who in zip(links, people):
        await joined_via(cog, g, link["code"], [who])
    assert [p.granted for p in people] == [["VISITOR"], ["CAP"], ["TABBY"], ["ORG"], ["EQUITY"]]
    assert all(len(p.roles) == 1 for p in people)               # never more than one role


async def test_invite_never_stacks_roles_on_someone_who_already_has_one():
    g = InviteGuild(); g.make_welcome()
    links = await ir.create_role_invites(g)
    cog = ir.InviteRolesCog(bot=None)
    rejoiner = member(g, 1)
    rejoiner.roles = [r for r in g.roles if r.name == "VISITOR"]
    await joined_via(cog, g, links[3]["code"], [rejoiner])      # joins via the [O] link
    assert [r.name for r in rejoiner.roles] == ["ORG"]


async def test_burst_of_joins_on_one_link_costs_one_lookup():
    g = InviteGuild(); g.make_welcome()
    links = await ir.create_role_invites(g)
    cog = ir.InviteRolesCog(bot=None)
    ms = [member(g, i) for i in range(10, 20)]
    g.live[links[0]["code"]].uses += 10
    calls = g.invite_calls
    for m in ms:
        await cog.on_member_join(m)
    await asyncio.sleep(0.3)
    assert g.invite_calls - calls == 1 and all(m.granted == ["VISITOR"] for m in ms)


async def test_ambiguous_joins_never_grant_roles():
    g = InviteGuild(); g.make_welcome()
    links = await ir.create_role_invites(g)
    cog = ir.InviteRolesCog(bot=None)
    a, b = member(g, 1), member(g, 2)
    g.live[links[0]["code"]].uses += 1                    # two different invites changed,
    g.live[links[3]["code"]].uses += 1                    # including an admin ([O]) one
    await cog.on_member_join(a); await cog.on_member_join(b)
    await asyncio.sleep(0.2)
    assert a.granted == [] and b.granted == []
    # more invite uses than joins we witnessed (someone joined while we weren't looking)
    c = member(g, 3)
    g.live[links[3]["code"]].uses += 2
    await cog.on_member_join(c)
    await asyncio.sleep(0.2)
    assert c.granted == []


async def test_normal_invite_and_untracked_guild_do_nothing():
    g = InviteGuild(); g.make_welcome()
    await ir.create_role_invites(g)
    g.live["plain"] = Inv("plain")
    ir._uses[g.id]["plain"] = 0
    cog = ir.InviteRolesCog(bot=None)
    m = member(g, 1)
    await joined_via(cog, g, "plain", [m])
    assert m.granted == []
    other = InviteGuild()
    await cog.on_member_join(member(other, 5))
    await asyncio.sleep(0.1)
    assert other.invite_calls == 0                        # untracked guild: zero requests
    assert member(g, 9, bot=True).bot


async def test_priming_only_touches_guilds_with_role_invites(monkeypatch):
    g1, g2 = InviteGuild(), InviteGuild()
    ir.config_manager.data[g1.id] = {"invite_roles": {"x": {"key": "visitor", "roles": ["VISITOR"]}}}
    cog = ir.InviteRolesCog(bot=types.SimpleNamespace(guilds=[g1, g2]))
    await cog.on_ready(); await cog.on_ready()            # reconnect must not re-prime
    await cog._prime_task
    assert g1.invite_calls == 1 and g2.invite_calls == 0 and g1.id in ir._tracked

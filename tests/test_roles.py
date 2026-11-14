import types
from unittest import mock

import discord
import pytest

import config
import roles_cog
import setup_cog
from ratelimit import RequestCoordinator
from tests.fakes import http_error


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    async def nosleep(_):
        return None
    monkeypatch.setattr(config, "notify", RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep}))


_ids = iter(range(100, 10_000))


def role(name, managed=False):
    return types.SimpleNamespace(id=next(_ids), name=name, managed=managed)


class Member:
    def __init__(self, roles=(), forbid_add=False, forbid_remove=False):
        self.id = next(_ids)
        self.roles = list(roles)
        self.forbid_add, self.forbid_remove = forbid_add, forbid_remove

    async def add_roles(self, *roles, reason=None):
        if self.forbid_add:
            raise http_error(403)
        self.roles += list(roles)

    async def remove_roles(self, *roles, reason=None):
        if self.forbid_remove:
            raise http_error(403)
        self.roles = [r for r in self.roles if r not in roles]


def interaction(member, guild_roles):
    i = mock.MagicMock()
    i.user = member
    i.guild = types.SimpleNamespace(roles=guild_roles)
    i.response.defer = mock.AsyncMock()
    i.response.send_message = mock.AsyncMock()
    i.followup.send = mock.AsyncMock()
    return i


def said(i):
    for call in (i.followup.send.await_args, i.response.send_message.await_args):
        if call is not None:
            return call.args[0] if call.args else call.kwargs.get("content", "")
    return ""


def guild_roles():
    return [role(n) for n in ("ORG", "CAP", "TABBY", "EQUITY", "INVITED ADJUDICATOR", "INDEPENDENT ADJUDICATOR", "DEBATER", "VISITOR")]


@pytest.mark.parametrize("name", ["DEBATER", "VISITOR", "INVITED ADJUDICATOR", "INDEPENDENT ADJUDICATOR"])
async def test_each_button_gives_its_role_without_any_assign_channel(name):
    roles = guild_roles()
    m = Member([role("@everyone")])
    i = interaction(m, roles)
    await roles_cog._assign_role(i, name)
    assert [r.name for r in m.roles if r.name == name] == [name]
    assert "You are now" in said(i) and name in said(i)


async def test_switching_replaces_the_old_role():
    roles = guild_roles()
    m = Member([next(r for r in roles if r.name == "VISITOR")])
    i = interaction(m, roles)
    await roles_cog._assign_role(i, "DEBATER")
    assert [r.name for r in m.roles] == ["DEBATER"] and "Switched" in said(i)


async def test_same_role_again_and_admin_holders_are_refused():
    roles = guild_roles()
    debater = next(r for r in roles if r.name == "DEBATER")
    m = Member([debater])
    i = interaction(m, roles)
    await roles_cog._assign_role(i, "DEBATER")
    assert "already" in said(i)
    admin = Member([next(r for r in roles if r.name == "TABBY")])
    j = interaction(admin, roles)
    await roles_cog._assign_role(j, "DEBATER")
    assert "admin role" in said(j) and [r.name for r in admin.roles] == ["TABBY"]


async def test_missing_role_and_hierarchy_problems_are_explained():
    i = interaction(Member(), [])
    await roles_cog._assign_role(i, "DEBATER")
    assert "/st" in said(i)
    roles = guild_roles()
    m = Member(forbid_add=True)
    j = interaction(m, roles)
    await roles_cog._assign_role(j, "DEBATER")
    assert "above the tournament roles" in said(j) and not m.roles
    visitor = next(r for r in roles if r.name == "VISITOR")
    k = interaction(Member([visitor], forbid_remove=True), roles)
    await roles_cog._assign_role(k, "DEBATER")
    assert "above the tournament roles" in said(k)


async def test_assign_kamla_role_reports_the_outcome():
    r = role("DEBATER")
    assert await config.assign_kamla_role(Member(), r) is True
    assert await config.assign_kamla_role(Member(forbid_add=True), r) is False


async def test_creator_gets_the_chosen_organiser_role_after_setup():
    roles = guild_roles()
    guild = types.SimpleNamespace(id=5, roles=roles)
    creator = Member()
    await setup_cog._assign_creator_role(guild, creator, "TABBY")
    assert [r.name for r in creator.roles] == ["TABBY"]
    await setup_cog._assign_creator_role(guild, creator, "ORG")                 # replaces: one role per person
    assert [r.name for r in creator.roles] == ["ORG"]
    await setup_cog._assign_creator_role(types.SimpleNamespace(id=6, roles=[]), Member(), "ORG")   # no role: no crash

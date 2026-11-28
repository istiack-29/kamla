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


# ── the redesigned panel ───────────────────────────────────────────────────────

def test_panel_is_a_select_menu_with_a_description_per_role():
    view = roles_cog.RoleSelectView()
    select = view.children[0]
    assert isinstance(select, discord.ui.Select) and select.custom_id == roles_cog.SELECT_ID
    assert [o.value for o in select.options] == ["INVITED ADJUDICATOR", "INDEPENDENT ADJUDICATOR", "DEBATER", "VISITOR"]
    assert all(o.description and len(o.description) <= 100 for o in select.options)
    assert view.is_persistent()                                         # survives a restart


def test_old_button_panels_keep_working_with_the_same_ids():
    ids = {b.custom_id for b in roles_cog.LegacyRoleView().children}
    assert ids == {"kamla:role:invited_adj", "kamla:role:independent_adj", "kamla:role:debater", "kamla:role:visitor"}
    assert roles_cog.LegacyRoleView().is_persistent()


async def test_select_callback_assigns_the_chosen_role(monkeypatch):
    seen = []

    async def fake(interaction, name):
        seen.append(name)
    monkeypatch.setattr(roles_cog, "_assign_role", fake)
    sel = roles_cog.RoleSelect()
    monkeypatch.setattr(type(sel), "values", property(lambda self: ["DEBATER"]))
    await sel.callback(mock.MagicMock())
    assert seen == ["DEBATER"]


async def test_kamla_needs_manage_roles_and_a_higher_role_and_says_so():
    roles = guild_roles()
    for r in roles:
        r.position = 5
    me = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_roles=False, administrator=False),
                               top_role=types.SimpleNamespace(position=9))
    i = interaction(Member(), roles)
    i.guild = types.SimpleNamespace(id=1, roles=roles, me=me)
    await roles_cog._assign_role(i, "DEBATER")
    assert "Manage Roles" in said(i)
    me.guild_permissions.manage_roles = True
    me.top_role.position = 2                                            # KAMLA's role is BELOW the tournament roles
    j = interaction(Member(), roles)
    j.guild = types.SimpleNamespace(id=1, roles=roles, me=me)
    await roles_cog._assign_role(j, "DEBATER")
    assert "drag **KAMLA's role to the top**" in said(j)


async def test_an_expired_interaction_is_handled_quietly():
    roles = guild_roles()
    m = Member()
    i = interaction(m, roles)
    i.response.defer = mock.AsyncMock(side_effect=discord.NotFound(mock.MagicMock(status=404), "Unknown interaction"))
    await roles_cog._assign_role(i, "DEBATER")                          # must not raise
    assert not m.roles


async def test_double_click_does_not_assign_twice():
    import asyncio
    roles = guild_roles()
    m = Member()
    i = interaction(m, roles)
    gate = asyncio.Event()

    async def slow_defer(**kw):
        await gate.wait()
    i.response.defer = slow_defer
    first = asyncio.create_task(roles_cog._assign_role(i, "DEBATER"))
    await asyncio.sleep(0.01)
    j = interaction(m, roles)
    await roles_cog._assign_role(j, "DEBATER")
    assert "already being processed" in said(j)
    gate.set()
    await first
    assert [r.name for r in m.roles] == ["DEBATER"]


def test_the_panel_never_creates_a_kamla_role():
    import pathlib
    src = pathlib.Path(roles_cog.__file__).read_text(encoding="utf-8")
    assert "create_role" not in src

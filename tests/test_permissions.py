import types
from unittest import mock

import discord
import pytest

import server_builder as sb
import setup_cog
import assign_cog
from ratelimit import RequestCoordinator
from tests.fakes import FakeGuild, FakeConfig


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    async def nosleep(_):
        return None
    monkeypatch.setattr(sb, "bulk", RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep}))
    monkeypatch.setattr(sb, "config_manager", FakeConfig())

    async def noop(*a, **k):
        return None
    for name in ("_post_meet_developer", "_post_welcome", "_post_how_to_use",
                 "_post_settings_panel", "_post_all_in_panel"):
        monkeypatch.setattr(sb, name, noop)


async def build(g, rooms=1):
    return await sb.build_server(g, "ap", rooms, "+06:00", "T", 1)


def kamla_roles(g):
    return [r for r in g.roles if r.name in {c["name"] for c in sb.ROLE_CONFIG}]


async def test_no_role_has_administrator_or_create_invite():
    g = FakeGuild()
    await build(g)
    assert len(kamla_roles(g)) == 8
    for r in kamla_roles(g):
        assert not r.permissions.administrator, r.name
        assert not r.permissions.create_instant_invite, r.name
        assert not r.permissions.manage_guild and not r.permissions.manage_roles
        assert not r.permissions.manage_channels and not r.permissions.kick_members and not r.permissions.ban_members


async def test_organisers_keep_their_facilities_others_do_not():
    g = FakeGuild()
    await build(g)
    by = {r.name: r.permissions for r in g.roles}
    for staff in ("ORG", "CAP", "TABBY", "EQUITY"):
        p = by[staff]
        assert p.manage_messages and p.move_members and p.mute_members and p.deafen_members and p.priority_speaker
    for other in ("INVITED ADJUDICATOR", "INDEPENDENT ADJUDICATOR", "DEBATER", "VISITOR"):
        p = by[other]
        assert not (p.manage_messages or p.move_members or p.mute_members or p.mention_everyone)
        assert p.view_channel and p.connect and p.speak and p.send_messages        # ordinary member abilities


async def test_everyone_cannot_invite_and_rerun_is_a_noop():
    g = FakeGuild()
    assert g.default_role.permissions.create_instant_invite
    await build(g)
    assert not g.default_role.permissions.create_instant_invite
    assert g.default_role.permissions.view_channel                  # only that one bit changed
    edits = g.default_role.edit_calls
    await build(g)
    assert g.default_role.edit_calls == edits


async def test_kamla_itself_can_still_create_invites():
    ow = sb._bot_ow(FakeGuild())
    assert ow.create_instant_invite is True


async def test_old_admin_roles_are_demoted_on_rerun():
    g = FakeGuild()
    for c in sb.ROLE_CONFIG:                                        # a server built by the old version
        await g.create_role(c["name"], permissions=discord.Permissions(administrator=True))
    await build(g)
    assert all(not r.permissions.administrator for r in kamla_roles(g))
    before = sum(r.edit_calls for r in kamla_roles(g))
    await build(g)
    assert sum(r.edit_calls for r in kamla_roles(g)) == before      # idempotent


async def test_organisers_still_see_every_private_channel_without_admin():
    """Administrator used to bypass channel overwrites. Prove every non-public channel grants the four
    organiser roles explicit view access, so removing the bit hides nothing from them."""
    g = FakeGuild()
    await build(g, rooms=2)
    staff = [r for r in g.roles if r.name in ("ORG", "CAP", "TABBY", "EQUITY")]
    checked = 0
    for ch in g.channels:
        if g.default_role in ch.overwrites and ch.overwrites[g.default_role].view_channel is True:
            continue                                                # public top-level channels
        for r in staff:
            assert ch.overwrites[r].view_channel is True, (ch.name, r.name)
        checked += 1
    assert checked > 40


async def test_get_role_mentions_server_owner_and_names_organiser_roles():
    g = FakeGuild()
    await build(g)
    sent = {}

    async def capture(channel, **kw):
        sent.update(kw)
    with mock.patch.object(sb, "_send", capture):
        await sb._post_get_role(g)
    text = sent["embed"].description + " ".join(f.name + f.value for f in sent["embed"].fields)
    assert "<@4242>" in text
    for label in ("CA", "Equity", "Tabby", "Org"):
        assert label in text


def inter(guild, roles=(), admin=False):
    user = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(administrator=admin),
                                 roles=[types.SimpleNamespace(name=r) for r in roles], id=1)
    i = mock.MagicMock(guild=guild, user=user)
    i.response.send_message = mock.AsyncMock()
    return i


async def test_rebuild_and_mention_enforce_organiser_roles_themselves():
    cog = setup_cog.SetupCog(bot=None)
    stranger = inter(FakeGuild(), roles=["DEBATER"])
    await cog.rebuild.callback(cog, stranger)
    assert "need an admin role" in stranger.response.send_message.await_args.args[0]

    acog = assign_cog.AssignCog(bot=None)
    s2 = inter(FakeGuild(), roles=["VISITOR"])
    await acog.mention.callback(acog, s2, types.SimpleNamespace(mention="@u"), mock.MagicMock())
    assert "Only ORG" in s2.response.send_message.await_args.args[0]

    # an organiser (no Administrator bit) is let through to the real logic
    ch = mock.MagicMock(); ch.send = mock.AsyncMock(); ch.mention = "#c"
    org = inter(FakeGuild(), roles=["TABBY"])
    await acog.mention.callback(acog, org, types.SimpleNamespace(mention="@u"), ch)
    ch.send.assert_awaited()


async def test_slash_commands_are_not_hidden_behind_the_administrator_gate(tmp_path, monkeypatch):
    import main
    monkeypatch.setenv("KAMLA_STATE_DIR", str(tmp_path))
    bot = main.KamlaBot()
    with mock.patch.object(type(bot.tree), "sync", new=mock.AsyncMock(return_value=[])):
        await bot.setup_hook()
    try:
        for name in ("st", "rebuild", "mention", "nicknames"):
            assert bot.tree.get_command(name).default_permissions is None, name
    finally:
        await bot.close()

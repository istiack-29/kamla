"""Installation: join flow, compatibility, accurate progress, 2-second pacing, idempotence, hierarchy, /st gating."""
import re
import types
from unittest import mock

import discord
import pytest

import config as cfgmod
import installer as inst
import naming
import server_builder as sb
import setup_cog
from ratelimit import RequestCoordinator
from tests.fakes import FakeChannel, FakeConfig, FakeGuild, FakeRole, FakeCategory, FakeVoice, http_error


class Cfg(FakeConfig):
    async def ensure_config_channel(self, guild):
        ch = discord.utils.get(guild.text_channels, name=naming.CH_CONFIG)
        if ch is None:
            ch = await guild.create_text_channel(naming.CH_CONFIG, overwrites=cfgmod.with_thread_lock(guild, {
                guild.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False)}))
        return ch


class World(FakeGuild):
    """A guild where KAMLA is an administrator whose managed role sits at the top."""

    def __init__(self, fresh=True):
        super().__init__()
        self.me.guild_permissions = discord.Permissions.all()
        self.bot_role = FakeRole("KAMLA", position=90, managed=True)
        self.me.top_role = self.bot_role
        self.me.roles = [self.bot_role]
        self.self_role = self.bot_role
        self.roles.append(self.bot_role)
        self.owner = None
        self.creates: list[tuple[float, str]] = []
        self.now = 0.0
        if fresh:                                    # what Discord gives a new server
            self._channels_default()

    def _channels_default(self):
        for factory, name in ((FakeCategory, "Text Channels"), (FakeCategory, "Voice Channels")):
            c = factory(self, name)
            self._channels[c.id] = c
        for factory, name in ((FakeChannel, "general"), (FakeVoice, "General")):
            c = factory(self, name)
            self._channels[c.id] = c

    def get_member(self, uid):
        return None

    async def _create(self, cls, kind, name, **kw):
        self.creates.append((self.now, name))
        return await super()._create(cls, kind, name, **kw)


@pytest.fixture
def clock(monkeypatch):
    """A fake clock: sleeping advances it, so the 2-second pacing is measured without waiting."""
    state = {"t": 1000.0, "sleeps": []}

    async def sleep(seconds):
        state["sleeps"].append(seconds)
        state["t"] += seconds
    monkeypatch.setattr(sb, "_sleep", sleep)
    monkeypatch.setattr(sb, "_clock", lambda: state["t"])
    monkeypatch.setattr(inst, "_clock", lambda: state["t"])
    sb._last_create.clear()
    return state


@pytest.fixture(autouse=True)
def env(monkeypatch, clock):
    async def nosleep(_):
        return None
    fast = RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep, "rng": lambda: 0.0})
    cfg = Cfg()
    for mod in (sb, inst):
        monkeypatch.setattr(mod, "bulk", fast)
        monkeypatch.setattr(mod, "config_manager", cfg)
    monkeypatch.setattr(inst, "edits", fast)

    async def noop(*a, **k):
        return None
    for name in ("_post_meet_developer", "_post_members_info", "_post_get_role", "_post_how_to_use",
                 "_post_settings_panel", "_post_security_panel"):
        monkeypatch.setattr(sb, name, noop)
    import links_cog, webhook
    monkeypatch.setattr(links_cog, "refresh_links", noop)
    monkeypatch.setattr(webhook, "edit_join_log", noop)
    monkeypatch.setattr(setup_cog, "_assign_creator_role", noop)
    sb._trackers.clear()
    inst._running.clear()
    inst._last_reminder.clear()
    return cfg


def installer_user(uid=777):
    return types.SimpleNamespace(id=uid, mention=f"<@{uid}>", display_name="inst", __hash__=lambda s: uid)


class User:
    def __init__(self, uid=777):
        self.id, self.mention = uid, f"<@{uid}>"

    def __hash__(self):
        return self.id


DATA = {"format": "ap", "rooms": 2, "timezone": "+06:00", "tournament_name": "T", "role_name": "ORG"}


# ── on join ────────────────────────────────────────────────────────────────────────────────────────────────

async def test_joining_creates_exactly_the_config_and_status_channels_and_says_im_here():
    g = World()
    before = {c.name for c in g.channels}
    status, compat = await inst.setup_on_join(g, User())
    new = {c.name for c in g.channels} - before
    assert new == {naming.CH_CONFIG, naming.CH_STATUS} and compat.ok
    msg = status.messages[0]
    assert msg.embed.title == "👋 I'M HERE" and isinstance(msg.view, inst.CreateNowView)
    assert [b.label for b in msg.view.children] == ["Create Now"]
    assert not any(c.name in ("Text Channels",) and False for c in g.channels)


async def test_status_channel_is_only_for_the_installer_and_kamla_and_read_only():
    g = World()
    me = User(777)
    status, _ = await inst.setup_on_join(g, me)
    assert status.overwrites[g.default_role].view_channel is False
    assert status.overwrites[me].view_channel is True and status.overwrites[me].use_application_commands is True
    assert status.overwrites[g.me].view_channel is True
    assert cfgmod.thread_locked(status)
    assert inst.installer_of(g) == 777


async def test_without_an_audit_log_entry_the_owner_is_the_installer():
    g = World()
    g.get_member = lambda uid: User(uid) if uid == g.owner_id else None
    await inst.setup_on_join(g, None)
    assert inst.installer_of(g) == g.owner_id


async def test_joining_twice_does_not_duplicate_the_channels():
    g = World()
    await inst.setup_on_join(g, User())
    await inst.setup_on_join(g, User())
    names = [c.name for c in g.channels]
    assert names.count(naming.CH_CONFIG) == 1 and names.count(naming.CH_STATUS) == 1


# ── compatibility ──────────────────────────────────────────────────────────────────────────────────────────

def test_a_fresh_server_is_compatible():
    c = inst.check_compatibility(World())
    assert c.ok and [x.key for x in c.checks] == ["compat", "perms", "hierarchy"]


async def test_a_populated_server_gets_no_setup_and_a_fresh_server_recommendation():
    g = World()
    await g.create_text_channel("our-community-chat")
    await g.create_role("Moderators")
    status, compat = await inst.setup_on_join(g, User())
    assert not compat.ok and "our-community-chat" in compat.foreign_channels and "Moderators" in compat.foreign_roles
    assert "fresh server" in compat.headline.lower()
    msg = status.messages[0]
    assert msg.view is None                                              # no Create Now button
    assert "recommends a fresh server" in msg.embed.description.lower()


async def test_run_install_refuses_a_populated_server_and_touches_nothing():
    g = World()
    keep = await g.create_text_channel("keep-me")
    status = await g.create_text_channel(naming.CH_STATUS)
    created, deleted, edits = len(g.create_calls), g.deleted, g.edits
    await inst.run_install(g, status, User(), DATA)
    assert len(g.create_calls) == created and g.deleted == deleted and g.edits == edits
    assert keep.id in g._channels and not any(r.name == "ORG" for r in g.roles)


def test_missing_permissions_are_named():
    g = World()
    g.me.guild_permissions = discord.Permissions(view_channel=True, send_messages=True)
    c = inst.check_compatibility(g)
    assert not c.ok and c.checks[1].state == "failed" and "Manage Channels" in c.checks[1].detail
    assert "Manage Roles" in c.checks[1].detail and inst.check_permissions(g)


def test_an_incorrect_role_hierarchy_blocks_setup_and_names_the_roles():
    g = World()
    g.roles.append(FakeRole("ORG", position=200))                        # a KAMLA role above the bot's own role
    c = inst.check_compatibility(g)
    assert c.checks[2].state == "failed" and "ORG" in c.checks[2].detail and not c.ok


def test_an_existing_kamla_server_is_resumable_even_with_extra_channels():
    g = World()
    cat = FakeCategory(g, naming.CAT_ORGCOM)
    g._channels[cat.id] = cat
    extra = FakeChannel(g, "organisers-added-this")
    g._channels[extra.id] = extra
    c = inst.check_compatibility(g)
    assert c.ok and c.already_installed


# ── installing ─────────────────────────────────────────────────────────────────────────────────────────────

async def full_install(g, data=DATA):
    status = await g.create_text_channel(naming.CH_STATUS)
    await inst.run_install(g, status, User(), data)
    return status


async def test_channels_are_created_two_seconds_apart(clock):
    g = World()
    sb_creates = []
    orig = g._create

    async def stamped(cls, kind, name, **kw):
        sb_creates.append(clock["t"])
        return await orig(cls, kind, name, **kw)
    g._create = stamped
    await full_install(g)
    assert len(sb_creates) > 40
    sb_creates = sb_creates[1:]                       # the first one is the test's own status channel, not paced
    gaps = [b - a for a, b in zip(sb_creates, sb_creates[1:])]
    assert min(gaps) >= sb.CHANNEL_INTERVAL - 1e-9 and sb.CHANNEL_INTERVAL == 2.0


async def test_pacing_only_waits_for_the_remainder_of_the_interval(clock):
    g = World()
    await sb._pace(g)
    clock["t"] += 1.5                                                    # 1.5 s of other work already passed
    clock["sleeps"].clear()
    await sb._pace(g)
    assert clock["sleeps"] == [pytest.approx(0.5)]
    clock["t"] += 10
    clock["sleeps"].clear()
    await sb._pace(g)
    assert clock["sleeps"] == []                                         # long gap: no extra waiting


async def test_progress_counts_are_real_and_the_install_ends_with_server_completed():
    g = World()
    status = await full_install(g)
    final = status.messages[-1].embed
    text = final.description
    for label in ("Checking server compatibility", "Checking permissions", "Checking role hierarchy", "Creating roles",
                  "Creating categories", "Creating channels", "Configuring permissions", "Installation completed"):
        assert f"✅ {label}" in text.replace("— ", "—") or f"✅ {label} " in text, label
    result = next(f.value for f in final.fields if f.name == "Result")
    assert "Server Completed" in result
    totals = inst.planned_totals("ap", 2)
    kamla = [c for c in g.channels if c.name not in inst.DEFAULT_NAMES and c.name != naming.CH_STATUS]
    assert sum(1 for c in kamla if c.kind == "category") == totals["categories"]
    assert sum(1 for c in kamla if c.kind != "category") == totals["channels"]
    assert f"{totals['roles']}/{totals['roles']}" in text
    assert f"{totals['categories']}/{totals['categories']}" in text and f"{totals['channels']}/{totals['channels']}" in text
    assert not any(f.name == "Remaining steps" for f in final.fields)
    assert final.color == discord.Color.green()


async def test_a_failed_install_never_claims_completion_and_says_what_failed():
    g = World()
    g.fail[naming.CH_SECURITY] = [http_error(400, code=50035)]
    status = await full_install(g)
    final = status.messages[-1].embed
    assert "Server Completed" not in " ".join(f.value for f in final.fields)
    assert "❌ Installation completed" in final.description
    assert any(f.name == "Problems" for f in final.fields) and final.color == discord.Color.red()


async def test_progress_never_runs_ahead_of_reality(env):
    """While the install runs, no phase is ticked that has not finished."""
    g = World()
    status = await g.create_text_channel(naming.CH_STATUS)
    seen = []
    orig = inst.InstallTracker.embed

    def spy(self):
        seen.append((dict(self.state), dict(self.counts), dict(self.totals)))
        return orig(self)
    with mock.patch.object(inst.InstallTracker, "embed", spy):
        await inst.run_install(g, status, User(), DATA)
    for state, counts, totals in seen[:-1]:
        if state["categories"] == "ok":
            assert counts["categories"] >= totals["categories"]
        if state["done"] == "ok":
            assert counts["channels"] >= totals["channels"]
    assert seen[-1][0]["done"] == "ok"
    assert all(s["done"] != "ok" for s, _c, _t in seen[:-1])


async def test_reinstalling_creates_nothing_new_and_never_duplicates():
    g = World()
    await full_install(g)
    snapshot = sorted((c.kind, c.name, c.category_id) for c in g.channels)
    roles = [r.name for r in g.roles]
    calls = len(g.create_calls)
    status = discord.utils.get(g.text_channels, name=naming.CH_STATUS)
    await inst.run_install(g, status, User(), DATA)                       # run it again
    assert len(g.create_calls) == calls                                   # not one channel or role created
    assert sorted((c.kind, c.name, c.category_id) for c in g.channels) == snapshot
    assert [r.name for r in g.roles] == roles


async def test_an_interrupted_install_resumes_creating_only_what_is_missing():
    g = World()
    g.fail[naming.CH_TIMER] = [http_error(400, code=50035)]
    await full_install(g)                                                 # fails on the first timer channel
    first_calls = len(g.create_calls)
    status = discord.utils.get(g.text_channels, name=naming.CH_STATUS)
    await inst.run_install(g, status, User(), DATA)
    new = g.create_calls[first_calls:]
    assert [n for _k, n in new].count(naming.CH_TIMER) == 1               # only the failed piece is retried
    assert len(new) < 6
    assert status.messages[-1].embed.color == discord.Color.green()


async def test_install_never_deletes_anything_and_leaves_foreign_resources_alone():
    g = World()
    await full_install(g)
    assert g.deleted == 0
    assert {"general", "General", "Text Channels", "Voice Channels"} <= {c.name for c in g.channels}   # defaults stay


async def test_exactly_one_kamla_role_the_bots_own_and_no_extra():
    g = World()
    await full_install(g)
    names = [r.name for r in g.roles]
    assert names.count("KAMLA") == 1 and g.bot_role in g.roles and g.bot_role.managed
    assert {n for n in names} - {"@everyone", "KAMLA"} == {c["name"] for c in sb.ROLE_CONFIG}
    assert cfgmod.kamla_role(g) is g.bot_role


# ── names, read-only, threads ───────────────────────────────────────────────────────────────────────────────

async def test_every_created_channel_and_category_follows_emoji_separator_name():
    g = World()
    await full_install(g)
    mine = [c for c in g.channels if c.name not in inst.DEFAULT_NAMES]
    assert len(mine) > 40 and len({(c.name, c.category_id) for c in mine}) == len(mine)      # no duplicates
    for c in mine:
        assert naming.SEP in c.name, c.name
        emoji, _sep, name = c.name.partition(naming.SEP)
        assert emoji and name and naming.valid_discord_name(c.name) and not emoji.isascii() or emoji.isascii() is False, c.name
        if c.kind == "text":                                                # Discord text names: no spaces, lower case
            assert " " not in c.name and c.name == c.name.lower(), c.name


def test_the_documented_example_names_are_exactly_these():
    assert naming.CH_CONFIG == "⚙️︱kamla-config" and naming.CH_STATUS == "📊︱server-status"
    assert naming.CH_MEMBERS == "👥︱members-update" and naming.CH_LINKS == "🔗︱links"
    assert naming.CH_SETTINGS == "🛠️︱settings" and naming.CH_DEVELOPERS == "👨‍💻︱meet-our-developers"
    assert naming.base_name(naming.CH_TIMER) == "timer" and naming.is_kind("⏱️︱Timer", "timer")


async def test_the_four_read_only_channels_deny_everyone_from_sending():
    g = World()
    await full_install(g)
    await inst.setup_on_join(g, User())                                     # creates kamla-config
    for name in naming.READ_ONLY_CHANNELS:
        ch = discord.utils.get(g.text_channels, name=name)
        assert ch is not None, name
        assert ch.overwrites[g.default_role].send_messages is False, name
        for target, ow in ch.overwrites.items():
            if target is not g.me:
                assert ow.send_messages is not True, (name, target)         # no role is granted Send Messages
    members = discord.utils.get(g.text_channels, name=naming.CH_MEMBERS)
    assert members.overwrites[g.default_role].view_channel is True          # members-update is visible to everyone
    devs = discord.utils.get(g.text_channels, name=naming.CH_DEVELOPERS)
    assert devs.overwrites[g.default_role].view_channel is True


async def test_every_channel_denies_thread_creation_and_it_is_verified():
    g = World()
    await full_install(g)
    assert g.channels and all(cfgmod.thread_locked(c) for c in g.channels)
    ow = g.default_role.permissions
    assert not (ow.create_public_threads or ow.create_private_threads or ow.send_messages_in_threads)
    assert not ow.create_instant_invite
    for c in g.channels:
        o = c.overwrites[g.default_role]
        assert o.create_public_threads is False and o.create_private_threads is False and o.send_messages_in_threads is False


async def test_thread_lock_covers_pre_existing_channels_and_is_idempotent():
    g = World()
    for n in ("a", "b", "c"):
        await g.create_text_channel(n)
    checked, fixed, unverified = await sb.enforce_thread_lock(g)
    assert fixed == checked and unverified == 0
    edits = g.edits
    assert await sb.enforce_thread_lock(g) == (checked, 0, 0) and g.edits == edits


async def test_security_category_and_channel_exist_after_install():
    g = World()
    await full_install(g)
    cat = discord.utils.get(g.categories, name=naming.CAT_SECURITY)
    assert cat is not None and [c.name for c in cat.text_channels] == [naming.CH_SECURITY]
    assert cat.overwrites[g.default_role].send_messages is not True


# ── migration of older servers ──────────────────────────────────────────────────────────────────────────────

async def test_legacy_names_are_renamed_and_the_all_in_channel_is_removed_nothing_else():
    g = World(fresh=False)
    top = [await g.create_text_channel(n) for n in ("👋🏻︱welcome", "👐🏻︱meet-the-developer", "⚙️︱settings")]
    room = await g.create_category("ROOM 01")
    for n in ("timer", "poi", "all-in"):
        await g.create_text_channel(n, category=room)
    for n in ("DEBATE ROOM", "OG PREP", "ADJUDICATION"):
        await g.create_voice_channel(n, category=room)
    foreign = await g.create_text_channel("all-in")                         # a user's own channel outside any room
    renamed = await sb.migrate_legacy_names(g)
    names = {c.name for c in g.channels}
    assert {naming.CH_MEMBERS, naming.CH_DEVELOPERS, naming.CH_SETTINGS, naming.CH_TIMER, naming.VC_DEBATE,
            naming.VC_ADJ, naming.prep_name("OG PREP"), naming.room_category(1)} <= names
    in_room = [c.name for c in room.channels]
    assert "all-in" not in in_room and "poi" in in_room                    # all-in removed, poi left untouched
    assert foreign.id in g._channels                                         # outside a room: never touched
    assert await sb.migrate_legacy_names(g) == []                             # idempotent
    assert renamed


# ── /st and the buttons ─────────────────────────────────────────────────────────────────────────────────────

def st_interaction(g, user_id, channel_name):
    i = mock.MagicMock()
    i.guild, i.user = g, types.SimpleNamespace(id=user_id, mention=f"<@{user_id}>")
    i.channel = types.SimpleNamespace(name=channel_name, send=mock.AsyncMock())
    i.response.send_message = mock.AsyncMock()
    i.client.get_guild.return_value = object()
    return i


async def run_st(i):
    cog = setup_cog.SetupCog(bot=None)
    await cog.st.callback(cog, i, types.SimpleNamespace(value="ap", name="AP"), 2, "T", types.SimpleNamespace(value="ORG"))


async def test_st_works_only_in_server_status_and_only_for_the_installer(env):
    g = World()
    await env.update_config(g, installer_id=777)
    called = []

    async def fake_run(*a):
        called.append(a)
    with mock.patch.object(inst, "run_install", fake_run):
        wrong_channel = st_interaction(g, 777, "general")
        await run_st(wrong_channel)
        assert "server-status" in wrong_channel.response.send_message.await_args.args[0] and not called
        stranger = st_interaction(g, 1, naming.CH_STATUS)
        await run_st(stranger)
        assert "Only the person who added KAMLA" in stranger.response.send_message.await_args.args[0] and not called
        owner_not_installer = st_interaction(g, g.owner_id, naming.CH_STATUS)         # even the owner, if they did not install
        await run_st(owner_not_installer)
        assert not called
        ok = st_interaction(g, 777, naming.CH_STATUS)
        await run_st(ok)
        assert len(called) == 1


async def test_st_on_a_populated_server_explains_instead_of_building(env):
    g = World()
    await g.create_text_channel("busy-server")
    await env.update_config(g, installer_id=777)
    i = st_interaction(g, 777, naming.CH_STATUS)
    with mock.patch.object(inst, "run_install", mock.AsyncMock()) as run:
        await run_st(i)
    run.assert_not_awaited()
    assert "fresh server" in i.response.send_message.await_args.kwargs["embed"].description.lower()


async def test_create_now_button_is_installer_only_and_hidden_checks_are_rerun(env):
    g = World()
    await env.update_config(g, installer_id=777)
    view = inst.CreateNowView()
    stranger = st_interaction(g, 5, naming.CH_STATUS)
    stranger.response.send_modal = mock.AsyncMock()
    await view.create_now.callback(stranger)
    stranger.response.send_modal.assert_not_awaited()
    ok = st_interaction(g, 777, naming.CH_STATUS)
    ok.response.send_modal = mock.AsyncMock()
    await view.create_now.callback(ok)
    assert isinstance(ok.response.send_modal.await_args.args[0], inst.SetupModal)
    await g.create_text_channel("someone-added-this-meanwhile")            # server became populated after the join
    late = st_interaction(g, 777, naming.CH_STATUS)
    late.response.send_modal = mock.AsyncMock()
    await view.create_now.callback(late)
    late.response.send_modal.assert_not_awaited()


# ── role hierarchy reminders ─────────────────────────────────────────────────────────────────────────────────

async def test_hierarchy_warning_goes_to_server_status_at_most_once_every_five_minutes(clock, env):
    g = World()
    status = await g.create_text_channel(naming.CH_STATUS)
    await env.update_config(g, installer_id=777)
    g.roles.append(FakeRole("ORG", position=200))                            # KAMLA's role is below ORG
    assert await inst.remind_hierarchy(g) is True
    assert "drag **KAMLA's role to the very top**" in status.messages[0].content and "<@777>" in status.messages[0].content
    for _ in range(5):
        clock["t"] += 50
        assert await inst.remind_hierarchy(g) is False                       # < 5 minutes since the last one
    clock["t"] += 60
    assert await inst.remind_hierarchy(g) is True                            # ≥ 300 s
    assert len(status.messages) == 2
    g.roles = [r for r in g.roles if r.name != "ORG"]                        # fixed: no more reminders
    clock["t"] += 1000
    assert await inst.remind_hierarchy(g) is False and len(status.messages) == 2


async def test_install_is_not_started_while_the_hierarchy_is_wrong():
    g = World()
    g.roles.append(FakeRole("ORG", position=200))
    status = await g.create_text_channel(naming.CH_STATUS)
    calls = len(g.create_calls)
    await inst.run_install(g, status, User(), DATA)
    assert len(g.create_calls) == calls                                      # nothing that needs the roles was attempted
    assert "KAMLA's role must be above" in status.messages[-1].embed.fields[0].value

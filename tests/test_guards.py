"""Strict read-only channels (owner included), thread prevention, members-update log, security safeguards."""
import asyncio
import types
from unittest import mock

import discord
import pytest

import config as cfgmod
import guards
import members_cog
import naming
import security_cog as sec
from ratelimit import RequestCoordinator
from tests.fakes import FakeConfig, FakeGuild, FakeChannel, FakeRole, http_error

BOT_ID = 31337


@pytest.fixture(autouse=True)
def env(monkeypatch):
    async def nosleep(_):
        return None
    fast = RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep, "rng": lambda: 0.0})
    cfg = FakeConfig()
    monkeypatch.setattr(guards, "notify", fast)
    monkeypatch.setattr(guards, "bulk", fast)
    monkeypatch.setattr(guards, "config_manager", cfg)
    monkeypatch.setattr(members_cog, "notify", fast)
    monkeypatch.setattr(sec, "notify", fast)
    monkeypatch.setattr(sec, "config_manager", cfg)
    monkeypatch.setattr(sec, "PANEL_DEBOUNCE", 0.0)
    sec._strikes.clear(); sec._kick_times.clear(); members_cog._recent.clear()
    return cfg


def bot():
    return types.SimpleNamespace(user=types.SimpleNamespace(id=BOT_ID), application_id=99)


def msg(channel, author_id, *, is_bot=False, webhook_id=None, app_id=None):
    m = mock.MagicMock()
    m.guild = channel.guild
    m.channel = channel
    m.author = types.SimpleNamespace(id=author_id, bot=is_bot, mention=f"<@{author_id}>")
    m.webhook_id, m.application_id = webhook_id, app_id
    m.delete = mock.AsyncMock()
    return m


def text_channel(name, guild=None):
    ch = mock.MagicMock(spec=discord.TextChannel)
    ch.name = name
    ch.guild = guild or types.SimpleNamespace(id=1)
    ch.send = mock.AsyncMock()
    return ch


# ── read-only channels ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(naming.READ_ONLY_CHANNELS))
@pytest.mark.parametrize("who", ["owner", "admin", "moderator", "member", "other_bot"])
async def test_every_message_by_anyone_is_deleted_in_read_only_channels(name, who):
    ch = text_channel(name)
    ids = {"owner": 10, "admin": 11, "moderator": 12, "member": 13, "other_bot": 14}
    m = msg(ch, ids[who], is_bot=(who == "other_bot"))
    await guards.GuardsCog(bot()).on_message(m)
    m.delete.assert_awaited_once()                               # the server owner (10) included


async def test_kamlas_own_messages_survive_in_read_only_channels():
    ch = text_channel(naming.CH_MEMBERS)
    own = msg(ch, BOT_ID, is_bot=True)
    hook = msg(ch, 555, is_bot=True, webhook_id=7, app_id=99)           # a webhook created by KAMLA's application
    foreign_hook = msg(ch, 556, is_bot=True, webhook_id=8, app_id=1234)
    cog = guards.GuardsCog(bot())
    for m in (own, hook, foreign_hook):
        await cog.on_message(m)
    own.delete.assert_not_awaited(); hook.delete.assert_not_awaited()
    foreign_hook.delete.assert_awaited_once()


async def test_ordinary_channels_are_never_touched():
    ch = text_channel(naming.CH_TIMER)
    m = msg(ch, 13)
    await guards.GuardsCog(bot()).on_message(m)
    m.delete.assert_not_awaited()
    dm = types.SimpleNamespace(guild=None, channel=mock.MagicMock(), author=types.SimpleNamespace(id=1))
    await guards.GuardsCog(bot()).on_message(dm)                    # a DM to the bot is not a guild message


async def test_a_failed_delete_does_not_crash_the_listener():
    ch = text_channel(naming.CH_SETTINGS)
    m = msg(ch, 13)
    m.delete = mock.AsyncMock(side_effect=http_error(403))
    await guards.GuardsCog(bot()).on_message(m)


def test_the_bot_keeps_manage_messages_in_read_only_channels():
    g = FakeGuild()
    import server_builder as sb
    assert sb._bot_ow(g).manage_messages is True
    # the hidden config channel is created with it as well
    import inspect
    assert "manage_messages=True" in inspect.getsource(cfgmod.ConfigManager.ensure_config_channel)


# ── threads ──────────────────────────────────────────────────────────────────────────────────────────────────

def thread(owner_id, guild):
    t = mock.MagicMock()
    t.owner_id, t.guild, t.id = owner_id, guild, 5
    t.delete = mock.AsyncMock()
    return t


async def test_unauthorised_threads_are_deleted_in_installed_servers(env):
    g = types.SimpleNamespace(id=1)
    env.data[1] = {"format": "ap"}
    mine, theirs = thread(BOT_ID, g), thread(13, g)
    cog = guards.GuardsCog(bot())
    await cog.on_thread_create(theirs)
    await cog.on_thread_create(mine)
    theirs.delete.assert_awaited_once()
    mine.delete.assert_not_awaited()


async def test_servers_without_a_kamla_installation_are_left_alone(env):
    g = types.SimpleNamespace(id=2)
    t = thread(13, g)
    await guards.GuardsCog(bot()).on_thread_create(t)
    t.delete.assert_not_awaited()


async def test_new_channels_get_the_thread_lock_immediately(env):
    g = FakeGuild()
    env.data[g.id] = {"format": "ap"}
    ch = FakeChannel(g, "brand-new")
    g._channels[ch.id] = ch
    await guards.GuardsCog(bot()).on_guild_channel_create(ch)
    assert cfgmod.thread_locked(ch)


# ── members-update log ──────────────────────────────────────────────────────────────────────────────────────

class Audit:
    def __init__(self, user, reason=None):
        self.user, self.reason = user, reason


class Logs(FakeGuild):
    def __init__(self):
        super().__init__()
        ch = FakeChannel(self, naming.CH_MEMBERS)
        self._channels[ch.id] = ch
        self.log = ch


def person(uid, bot_=False):
    p = types.SimpleNamespace(id=uid, bot=bot_, mention=f"<@{uid}>", display_avatar=types.SimpleNamespace(url="u"),
                              created_at=discord.utils.utcnow(), guild=None)
    p.__str__ = lambda self=p: f"user{uid}"
    return p


@pytest.fixture
def audit(monkeypatch):
    entries = {}

    async def find(guild, action, user_id):
        return entries.get((action, user_id))
    monkeypatch.setattr(members_cog, "find_audit_entry", find)
    return entries


def titles(g):
    return [m.embed.title for m in g.log.messages]


async def test_join_leave_ban_unban_are_each_logged_once(audit):
    g = Logs()
    cog = members_cog.MembersCog(bot())
    m = person(5); m.guild = g
    await cog.on_member_join(m)
    await cog.on_member_remove(m)
    mod = person(900)
    audit[(discord.AuditLogAction.ban, 6)] = Audit(mod, "spam")
    u = person(6); u.guild = g
    await cog.on_member_remove(u)                                  # Discord fires this for a ban too…
    await cog.on_member_ban(g, u)                                  # …so only on_member_ban logs it
    audit[(discord.AuditLogAction.unban, 6)] = Audit(mod)
    await cog.on_member_unban(g, u)
    assert titles(g) == ["📥 Member joined", "📤 Member left", "🔨 Member banned", "♻️ Member unbanned"]
    ban = g.log.messages[2].embed
    assert {f.name: f.value for f in ban.fields} == {"Moderator": "<@900>", "Reason": "spam"}
    assert {f.name: f.value for f in g.log.messages[3].embed.fields}["Moderator"] == "<@900>"


async def test_kick_is_reported_as_a_kick_with_the_moderator(audit):
    g = Logs()
    audit[(discord.AuditLogAction.kick, 8)] = Audit(person(901), "rules")
    m = person(8); m.guild = g
    await members_cog.MembersCog(bot()).on_member_remove(m)
    assert titles(g) == ["👢 Member kicked"] and g.log.messages[0].embed.fields[0].value == "<@901>"


async def test_moderator_is_not_invented_when_the_audit_log_has_nothing(audit):
    g = Logs()
    u = person(9)
    await members_cog.MembersCog(bot()).on_member_ban(g, u)
    f = {x.name: x.value for x in g.log.messages[0].embed.fields}
    assert f["Moderator"] == "Not available"


async def test_duplicate_gateway_events_are_logged_once(audit):
    g = Logs()
    m = person(5); m.guild = g
    cog = members_cog.MembersCog(bot())
    await cog.on_member_join(m); await cog.on_member_join(m)
    assert titles(g) == ["📥 Member joined"]


async def test_bots_are_labelled_and_a_missing_channel_is_harmless(audit):
    g = Logs()
    b = person(77, bot_=True); b.guild = g
    await members_cog.MembersCog(bot()).on_member_join(b)
    assert titles(g) == ["🤖 Bot added"]
    empty = FakeGuild()
    c = person(1); c.guild = empty
    await members_cog.MembersCog(bot()).on_member_join(c)           # no members-update channel: nothing, no crash


async def test_audit_lookup_requires_the_view_audit_log_permission():
    g = FakeGuild()
    g.me.guild_permissions = discord.Permissions(view_channel=True)
    assert await members_cog.find_audit_entry(g, discord.AuditLogAction.ban, 1) is None


# ── security ────────────────────────────────────────────────────────────────────────────────────────────────

class SecGuild(FakeGuild):
    def __init__(self):
        super().__init__()
        self.me.guild_permissions = discord.Permissions.all()
        self.me.top_role = FakeRole("KAMLA", position=50, managed=True)
        ch = FakeChannel(self, naming.CH_SECURITY)
        self._channels[ch.id] = ch
        self.sec = ch


class Bot_:
    """A bot account that is allowed to be moderated."""
    def __init__(self, uid, g, position=1):
        self.id, self.bot, self.guild = uid, True, g
        self.mention, self.top_role = f"<@{uid}>", types.SimpleNamespace(position=position)
        self.kicked = False
        self.deleted = False

    def __str__(self):
        return f"bot{self.id}"

    async def kick(self, reason=None):
        self.kicked = True


def sec_msg(g, author):
    m = mock.MagicMock()
    m.guild, m.channel, m.author = g, g.sec, author
    m.webhook_id = m.application_id = None
    m.delete = mock.AsyncMock()
    return m


async def say(cog, g, author):
    m = sec_msg(g, author)
    await cog.on_message(m)
    await asyncio.sleep(0.01)
    return m


def test_the_panel_is_a_prominent_warning_with_mentions_and_a_live_counter():
    g = SecGuild()
    g.roles.append(FakeRole("ORG", position=3))
    e = sec.build_security_embed(g, {"security": {"kicks": 3, "deleted": 12, "alerts": 1}})
    assert "SECURITY NOTICE" in e.title and "DO NOT WRITE" in e.description and e.color == discord.Color.red()
    by = {f.name: f.value for f in e.fields}
    assert by["🦵 Bots removed"] == "**3**" and by["🗑️ Messages deleted"] == "**12**" and by["🔔 Alerts"] == "**1**"
    assert f"<@{g.owner_id}>" in by["Report / questions"] and "ORG" in str(g.roles[-1].name)
    assert "deleted" in by["What can happen"] and "removed from the server" in by["What can happen"]


async def test_a_person_is_never_moderated_only_their_message_is_deleted(env):
    g = SecGuild()
    cog = sec.SecurityCog(bot())
    human = types.SimpleNamespace(id=13, bot=False, mention="<@13>")
    for _ in range(5):
        m = await say(cog, g, human)
        m.delete.assert_awaited_once()
    c = sec.counters(env.data[g.id])
    assert c["deleted"] == 5 and c["kicks"] == 0


async def test_a_bot_is_warned_first_then_removed_on_the_second_message(env):
    g = SecGuild()
    cog = sec.SecurityCog(bot())
    rogue = Bot_(500, g)
    await say(cog, g, rogue)
    assert not rogue.kicked and sec.counters(env.data[g.id])["kicks"] == 0
    await say(cog, g, rogue)
    assert rogue.kicked and sec.counters(env.data[g.id])["kicks"] == 1
    assert any("Removed bot" in (m.content or "") for m in g.sec.messages)


async def test_safeguards_allowlist_owner_higher_role_and_missing_permission(env):
    g = SecGuild()
    cog = sec.SecurityCog(bot())
    env.data[g.id] = {"security_allowlist": ["501"]}
    trusted = Bot_(501, g)
    above = Bot_(502, g, position=60)                                  # ranked above KAMLA
    owner_bot = Bot_(g.owner_id, g)
    for b in (trusted, above, owner_bot):
        await say(cog, g, b); await say(cog, g, b)
        assert not b.kicked
    assert sec.counters(env.data[g.id])["alerts"] >= 2                 # un-kickable ones raise an alert for the organisers
    g.me.guild_permissions = discord.Permissions(view_channel=True)
    nokick = Bot_(503, g)
    await say(cog, g, nokick); await say(cog, g, nokick)
    assert not nokick.kicked


async def test_kick_circuit_breaker_stops_accidental_mass_moderation(env):
    g = SecGuild()
    cog = sec.SecurityCog(bot())
    bots = [Bot_(600 + i, g) for i in range(6)]
    for b in bots:
        await say(cog, g, b); await say(cog, g, b)
    assert sum(b.kicked for b in bots) == sec.MAX_KICKS == 3
    assert sec.counters(env.data[g.id])["kicks"] == 3


async def test_strikes_expire(env, monkeypatch):
    g = SecGuild()
    t = {"now": 0.0}
    monkeypatch.setattr(sec, "_clock", lambda: t["now"])
    cog = sec.SecurityCog(bot())
    b = Bot_(700, g)
    await say(cog, g, b)
    t["now"] += sec.STRIKE_WINDOW + 1                                   # the first message is long forgotten
    await say(cog, g, b)
    assert not b.kicked


async def test_kamlas_own_messages_in_the_security_channel_are_kept(env):
    g = SecGuild()
    m = sec_msg(g, types.SimpleNamespace(id=BOT_ID, bot=True, mention="x"))
    await sec.SecurityCog(bot()).on_message(m)
    m.delete.assert_not_awaited()


async def test_counters_are_persisted_and_the_panel_embed_is_refreshed(env):
    g = SecGuild()
    panel = await sec.post_security_panel(g.sec)
    assert env.data[g.id]["security"]["message_id"] == panel.id
    await sec._bump(g, kicks=2, deleted=5)
    await asyncio.sleep(0.05)
    assert env.data[g.id]["security"]["kicks"] == 2                      # stored in the config: survives restarts
    shown = {f.name: f.value for f in panel.embed.fields}
    assert shown["🦵 Bots removed"] == "**2**" and shown["🗑️ Messages deleted"] == "**5**"

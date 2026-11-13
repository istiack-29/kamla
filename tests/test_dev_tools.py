import asyncio
import re
import types
from unittest import mock

import discord
import pytest

import devtools
import invite_roles_cog as ir
import webhook
from ratelimit import RequestCoordinator
from tests.fakes import FakeConfig, FakeGuild
from tests.test_invites import Inv, InviteGuild

OWNER = 777


@pytest.fixture(autouse=True)
def env(monkeypatch):
    async def nosleep(_):
        return None
    fast = RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep})
    monkeypatch.setattr(ir, "bulk", fast)
    monkeypatch.setattr(ir, "notify", fast)
    monkeypatch.setattr(ir, "BATCH_DELAY", 0.02)
    monkeypatch.setattr(ir, "config_manager", FakeConfig())
    ir._uses.clear()
    ir._tracked.clear()
    devtools.reset_owner_cache()
    monkeypatch.setenv("OWNER_ID", str(OWNER))


class DevGuild(InviteGuild):
    def __init__(self):
        super().__init__()
        self.members_by_id = {}
        self.me.top_role = types.SimpleNamespace(position=10)
        self.owner = None

    def get_member(self, i):
        return self.members_by_id.get(i)


def person(uid, guild):
    m = types.SimpleNamespace(id=uid, guild=guild, bot=False, roles=[], granted=[])

    async def add_roles(*roles, reason=None):
        m.roles += list(roles)
        m.granted += [r.name for r in roles]
    m.add_roles = add_roles
    return m


# ── developer invite + role ───────────────────────────────────────────────────

async def test_dev_invite_creates_visible_admin_role_and_gated_invite():
    g = DevGuild()
    g.make_welcome()
    url = await ir.ensure_dev_invite(g, OWNER)
    role = next(r for r in g.roles if r.name == "KAMLA")
    assert role.permissions.administrator and url.startswith("https://discord.gg/")
    entry = next(v for v in ir.config_manager.data[g.id]["invite_roles"].values() if v["key"] == "dev")
    assert entry["only_user"] == OWNER and entry["roles"] == ["KAMLA"]
    assert await ir.ensure_dev_invite(g, OWNER) == url and len(g.live) == 1          # idempotent


async def test_only_the_developer_gets_the_role_through_the_invite():
    g = DevGuild()
    g.make_welcome()
    url = await ir.ensure_dev_invite(g, OWNER)
    code = url.rsplit("/", 1)[1]
    cog = ir.InviteRolesCog(bot=None)
    stranger, dev = person(1, g), person(OWNER, g)
    for who in (stranger, dev):
        g.live[code].uses += 1
        await cog.on_member_join(who)
        await asyncio.sleep(0.2)
    assert stranger.granted == [] and dev.granted == ["KAMLA"]


async def test_role_is_given_directly_when_the_developer_is_already_inside():
    g = DevGuild()
    g.make_welcome()
    dev = person(OWNER, g)
    g.members_by_id[OWNER] = dev
    await ir.ensure_dev_invite(g, OWNER)
    assert dev.granted == ["KAMLA"]
    g.roles = [r for r in g.roles if r.name != "KAMLA"]                               # a rebuild wiped the role
    dev.roles.clear(); dev.granted.clear()
    await ir.ensure_dev_invite(g, OWNER)
    assert dev.granted == ["KAMLA"]


async def test_dev_invite_survives_the_tournament_invites_being_recreated():
    g = DevGuild()
    g.make_welcome()
    url = await ir.ensure_dev_invite(g, OWNER)
    await ir.create_role_invites(g)
    stored = ir.config_manager.data[g.id]["invite_roles"]
    assert any(v["key"] == "dev" for v in stored.values()) and sum(1 for v in stored.values() if v["key"] != "dev") == 5
    assert url.rsplit("/", 1)[1] in stored


# ── webhook: buttons instead of a link ────────────────────────────────────────

def log_guild():
    g = types.SimpleNamespace(id=31, name="Kijani", icon=None, owner=None, member_count=3,
                              created_at=discord.utils.utcnow())
    return g


async def test_log_through_the_bot_has_role_link_button_and_leave_button(monkeypatch):
    webhook._join_message_ids.clear(); webhook._bot_messages.clear()
    sent = {}

    class Channel:
        async def send(self, embed=None, view=None):
            sent.update(embed=embed, view=view)
            return types.SimpleNamespace(id=555, edit=mock.AsyncMock())
    monkeypatch.setattr(webhook, "_log_channel", mock.AsyncMock(return_value=Channel()))
    monkeypatch.setattr(webhook, "_dev_invite", mock.AsyncMock(return_value="https://discord.gg/abc"))
    g = log_guild()
    await webhook.send_join_log(g, None)
    fields = " ".join(f["name"] + str(f["value"]) for f in sent["embed"].to_dict()["fields"])
    assert "discord.gg" not in fields and "Invite" not in fields                       # no raw link any more
    items = sent["view"].children
    link = next(i for i in items if getattr(i, "url", None))
    assert link.label == "KAMLA Role" and link.url == "https://discord.gg/abc"
    leave = next(i for i in items if getattr(i, "custom_id", "") == f"kamla:dev:leave:{g.id}")
    assert leave.item.label == "Leave the server"
    assert webhook._join_message_ids[g.id] == "555"


async def test_edit_and_delete_go_through_the_same_message(monkeypatch):
    webhook._join_message_ids.clear(); webhook._bot_messages.clear()
    msg = types.SimpleNamespace(id=9, edit=mock.AsyncMock())
    g = log_guild()
    webhook._bot_messages[g.id] = msg
    webhook._join_message_ids[g.id] = "9"
    monkeypatch.setattr(webhook, "_dev_invite", mock.AsyncMock(return_value="https://discord.gg/abc"))
    await webhook.edit_join_log(g, {"format": "ap", "rooms": 3, "locked": True, "tournament_name": "T"})
    assert msg.edit.await_count == 1 and msg.edit.await_args.kwargs["view"] is not None
    await webhook.mark_guild_deleted(g)
    assert msg.edit.await_args.kwargs["view"] is None and g.id not in webhook._join_message_ids


async def test_webhook_fallback_uses_a_link_button_only(monkeypatch):
    webhook._join_message_ids.clear(); webhook._bot_messages.clear()
    posted = {}

    async def fake_post(payload):
        posted.update(payload)
        return "321"
    monkeypatch.setattr(webhook, "_post", fake_post)
    monkeypatch.setattr(webhook, "_log_channel", mock.AsyncMock(return_value=None))     # KAMLA is not in the log server
    monkeypatch.setattr(webhook, "_dev_invite", mock.AsyncMock(return_value="https://discord.gg/abc"))
    g = log_guild()
    await webhook.send_join_log(g, None)
    button = posted["components"][0]["components"][0]
    assert button["style"] == 5 and button["url"] == "https://discord.gg/abc" and button["label"] == "KAMLA Role"
    assert "Leave button" in posted["embeds"][0]["footer"]["text"]                       # tells the developer how to enable it
    assert webhook._join_message_ids[g.id] == "321"


async def test_no_invite_means_no_role_button(monkeypatch):
    assert webhook._webhook_components("") == []


# ── leave button ──────────────────────────────────────────────────────────────

def test_leave_button_template_roundtrip():
    m = re.fullmatch(devtools.LeaveButton.__discord_ui_compiled_template__, "kamla:dev:leave:1234")
    assert m and m["gid"] == "1234"


async def test_only_the_developer_may_press_leave():
    item = devtools.LeaveButton(31)
    inter = mock.MagicMock()
    inter.client = types.SimpleNamespace()
    inter.response.send_message = mock.AsyncMock()
    inter.user = types.SimpleNamespace(id=1)
    assert await item.interaction_check(inter) is False
    inter.user = types.SimpleNamespace(id=OWNER)
    assert await item.interaction_check(inter) is True


class LeaveGuild:
    def __init__(self, general=True):
        self.id, self.name, self.left = 31, "Kijani", False
        self.me = types.SimpleNamespace()
        self.sent = []
        ch = types.SimpleNamespace(name="📨︱ga-text", permissions_for=lambda me: types.SimpleNamespace(send_messages=True))
        other = types.SimpleNamespace(name="random", permissions_for=lambda me: types.SimpleNamespace(send_messages=True))
        self.text_channels = [other] + ([ch] if general else [])
        self.system_channel = None
        for c in self.text_channels:
            async def send(content, allowed_mentions=None, c=c):
                self.sent.append((c.name, content, allowed_mentions))
            c.send = send

    async def leave(self):
        self.left = True


async def test_leave_announces_in_general_chat_then_leaves():
    g = LeaveGuild()
    assert await devtools.leave_guild(g) is True
    name, text, mentions = g.sent[0]
    assert name == "📨︱ga-text" and text.startswith("@everyone") and "Unauthorised activity detected" in text
    assert mentions.everyone is True and g.left


async def test_leave_falls_back_to_another_channel_and_survives_a_failed_notice():
    g = LeaveGuild(general=False)
    assert await devtools.leave_guild(g) and g.sent[0][0] == "random"
    g2 = LeaveGuild()
    g2.text_channels[1].send = mock.AsyncMock(side_effect=RuntimeError("boom"))
    assert await devtools.leave_guild(g2) and g2.left


async def test_leave_needs_confirmation():
    item = devtools.LeaveButton(31)
    g = LeaveGuild()
    inter = mock.MagicMock()
    inter.client = types.SimpleNamespace(get_guild=lambda i: g)
    inter.response.send_message = mock.AsyncMock()
    await item.callback(inter)
    assert not g.left and isinstance(inter.response.send_message.await_args.kwargs["view"], devtools.ConfirmLeaveView)


# ── caching, channel detection, owner lookup ─────────────────────────────────

async def test_dev_invite_is_cached_between_log_edits(monkeypatch):
    webhook._dev_invite_cache.clear()
    calls = []

    async def fake_ensure(guild, owner):
        calls.append(owner)
        return "https://discord.gg/x"
    import importlib   # an unloaded cog is removed from sys.modules, so patch whatever webhook will import
    monkeypatch.setattr(importlib.import_module("invite_roles_cog"), "ensure_dev_invite", fake_ensure)
    monkeypatch.setattr(webhook, "_bot", types.SimpleNamespace())
    g = log_guild()
    for _ in range(5):
        assert await webhook._dev_invite(g) == "https://discord.gg/x"
    assert len(calls) == 1                                       # five log edits, one lookup
    webhook.forget_dev_invite(g.id)
    await webhook._dev_invite(g)
    assert len(calls) == 2                                       # a rebuild forces a fresh one


async def test_unchanged_dev_invite_does_not_rewrite_the_config():
    g = DevGuild()
    g.make_welcome()
    await ir.ensure_dev_invite(g, OWNER)
    snapshot = dict(ir.config_manager.data[g.id])
    ir.config_manager.update_config = mock.AsyncMock()           # any further write would be a bug
    await ir.ensure_dev_invite(g, OWNER)
    ir.config_manager.update_config.assert_not_awaited()
    assert ir.config_manager.data[g.id] == snapshot


async def test_log_channel_detection(monkeypatch):
    class Resp:
        status = 200

        async def json(self, content_type=None):
            return {"channel_id": "123"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def get(self, url):
            assert "/webhooks/55/tok" in url
            return Resp()

    async def get_session():
        return Session()
    channel = types.SimpleNamespace(guild=types.SimpleNamespace(me=object()),
                                    permissions_for=lambda me: types.SimpleNamespace(send_messages=True, embed_links=True))
    monkeypatch.setattr(webhook, "WEBHOOK_URL", "https://discord.com/api/webhooks/55/tok")
    monkeypatch.setattr(webhook, "_get_session", get_session)
    monkeypatch.setattr(webhook, "_bot", types.SimpleNamespace(get_channel=lambda i: channel if i == 123 else None))
    webhook._channel_cache.update(at=float("-inf"), channel=None)
    assert await webhook._log_channel() is channel
    webhook._bot = types.SimpleNamespace(get_channel=lambda i: None)          # KAMLA not in the log server
    webhook._channel_cache.update(at=float("-inf"), channel=None)
    assert await webhook._log_channel() is None
    webhook._channel_cache.update(at=float("-inf"), channel=None)


async def test_owner_lookup_falls_back_to_the_application_owner(monkeypatch):
    monkeypatch.delenv("OWNER_ID", raising=False)
    devtools.reset_owner_cache()
    bot = types.SimpleNamespace(application_info=mock.AsyncMock(
        return_value=types.SimpleNamespace(team=None, owner=types.SimpleNamespace(id=4321))))
    assert await devtools.get_owner_id(bot) == 4321
    devtools.reset_owner_cache()
    team_bot = types.SimpleNamespace(application_info=mock.AsyncMock(
        return_value=types.SimpleNamespace(team=types.SimpleNamespace(owner_id=999), owner=None)))
    assert await devtools.get_owner_id(team_bot) == 999


# ── developer-invite joins get no welcome message ────────────────────────────

async def test_join_outcome_reports_which_invite_was_used():
    g = DevGuild()
    g.make_welcome()
    url = await ir.ensure_dev_invite(g, OWNER)
    links = await ir.create_role_invites(g)
    code = url.rsplit("/", 1)[1]
    cog = ir.InviteRolesCog(bot=None)

    dev, visitor = person(OWNER, g), person(5, g)
    g.live[code].uses += 1
    await cog.on_member_join(dev)
    assert await ir.join_outcome(dev) == "dev"
    g.live[links[0]["code"]].uses += 1
    await cog.on_member_join(visitor)
    assert await ir.join_outcome(visitor) == "visitor"


async def test_join_outcome_works_whichever_listener_runs_first():
    g = DevGuild()
    g.make_welcome()
    url = await ir.ensure_dev_invite(g, OWNER)
    cog = ir.InviteRolesCog(bot=None)
    dev = person(OWNER, g)
    g.live[url.rsplit("/", 1)[1]].uses += 1
    waiter = asyncio.create_task(ir.join_outcome(dev))          # the welcome listener asks BEFORE the tracker runs
    await asyncio.sleep(0.01)
    await cog.on_member_join(dev)
    assert await waiter == "dev"


async def test_join_outcome_is_immediate_when_not_tracked_and_none_for_normal_invites():
    g = DevGuild()
    stranger = person(9, g)
    assert await ir.join_outcome(stranger) is None              # no role invites in this server: no waiting
    g.make_welcome()
    await ir.create_role_invites(g)
    g.live["plain"] = Inv("plain")
    ir._uses[g.id]["plain"] = 0
    cog = ir.InviteRolesCog(bot=None)
    g.live["plain"].uses += 1
    await cog.on_member_join(stranger)
    assert await ir.join_outcome(stranger) is None


async def test_ambiguous_join_never_counts_as_developer():
    g = DevGuild()
    g.make_welcome()
    url = await ir.ensure_dev_invite(g, OWNER)
    links = await ir.create_role_invites(g)
    cog = ir.InviteRolesCog(bot=None)
    a, b = person(OWNER, g), person(8, g)
    g.live[url.rsplit("/", 1)[1]].uses += 1
    g.live[links[0]["code"]].uses += 1
    await cog.on_member_join(a)
    await cog.on_member_join(b)
    assert await ir.join_outcome(a) is None and await ir.join_outcome(b) is None


async def test_welcome_message_is_skipped_for_the_developer_link_only(monkeypatch):
    import welcome_cog
    sent = []

    class Chan:
        name = welcome_cog.WELCOME_CHANNEL_NAME

        async def send(self, **kw):
            sent.append(kw)

    class G:
        id = 77
        system_channel = None
        text_channels = [Chan()]
        member_count = 3
        name = "G"
        banner = icon = None
        me = types.SimpleNamespace(display_avatar=types.SimpleNamespace(url="https://x/m.png"))

    member = types.SimpleNamespace(id=1, guild=G(), mention="<@1>", bot=False,
                                   display_avatar=types.SimpleNamespace(url="https://x/a.png"))
    cog = welcome_cog.WelcomeCog(bot=None)
    import importlib
    mod = importlib.import_module("invite_roles_cog")
    monkeypatch.setattr(mod, "join_outcome", mock.AsyncMock(return_value="dev"))
    await cog.on_member_join(member)
    assert sent == []                                            # developer link: no welcome
    monkeypatch.setattr(mod, "join_outcome", mock.AsyncMock(return_value="visitor"))
    await cog.on_member_join(member)
    assert len(sent) == 1                                        # any other link: normal welcome
    monkeypatch.setattr(mod, "join_outcome", mock.AsyncMock(side_effect=RuntimeError("boom")))
    await cog.on_member_join(member)
    assert len(sent) == 2                                        # a failure here never swallows a welcome

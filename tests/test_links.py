"""🔗︱links: permanent role invitations (Discord role_ids), fallback, no DMs, no previews, no duplicates."""
import types

import discord
import pytest

import config as cfgmod
import links_cog as lk
from naming import CH_LINKS
from ratelimit import RequestCoordinator
from tests.fakes import FakeChannel, FakeConfig, FakeGuild, FakeRole, http_error


class Inv:
    def __init__(self, code, uses=0):
        self.code, self.uses = code, uses
        self.url = f"https://discord.gg/{code}"
        self.guild = None
        self.deleted = False

    async def delete(self, reason=None):
        self.deleted = True


class LinkGuild(FakeGuild):
    def __init__(self):
        super().__init__()
        self.live: dict[str, Inv] = {}
        self.n = 0
        self.native_payloads: list[dict] = []
        self.self_role = FakeRole("KAMLA", position=50, managed=True)     # the bot's OWN role
        self.me.roles = [self.self_role]
        for name in cfgmod.KAMLA_ROLE_NAMES:
            self.roles.append(FakeRole(name, position=len(self.roles)))
        ch = FakeChannel(self, CH_LINKS)
        self._channels[ch.id] = ch
        self.links = ch

        async def create_invite(**kw):
            assert kw["max_age"] == 0 and kw["max_uses"] == 0 and kw["unique"] is True     # permanent + own link
            return self._new()
        ch.create_invite = create_invite

    def _new(self):
        self.n += 1
        inv = Inv(f"code{self.n}")
        self.live[inv.code] = inv
        return inv

    async def invites(self):
        return list(self.live.values())


@pytest.fixture
def env(monkeypatch):
    async def nosleep(_):
        return None
    fast = RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep, "rng": lambda: 0.0})
    cfg = FakeConfig()
    monkeypatch.setattr(lk, "bulk", fast)
    monkeypatch.setattr(lk, "config_manager", cfg)
    import invite_roles_cog as ir
    monkeypatch.setattr(ir, "config_manager", cfg)
    monkeypatch.setattr(ir, "bulk", fast)
    ir._uses.clear(); ir._tracked.clear()
    return cfg


@pytest.fixture
def native(monkeypatch):
    """Discord accepts role_ids for every role except the bot's own (managed) role."""
    calls = []

    async def http_create(channel, payload, reason):
        calls.append(payload)
        role_id = int(payload["role_ids"][0])
        if role_id == channel.guild.self_role.id:
            raise http_error(400, code=50035, text="Invalid role")
        inv = channel.guild._new()
        return {"code": inv.code}
    monkeypatch.setattr(lk, "_http_create_invite", http_create)
    return calls


async def test_every_available_role_gets_a_permanent_native_link(env, native):
    g = LinkGuild()
    rows = await lk.refresh_links(g)
    assert [r["key"] for r in rows] == ["org", "captain", "tabby", "equity", "invited", "independent", "debater",
                                        "visitor", "kamla"]
    assert all(r["available"] for r in rows)
    tournament = [r for r in rows if r["key"] != "kamla"]
    assert all(r["native"] for r in tournament)
    assert all(p["max_age"] == 0 and p["max_uses"] == 0 and p["unique"] and len(p["role_ids"]) == 1 for p in native)
    assert {p["role_ids"][0] for p in native} >= {str(r["role"].id) for r in tournament}


async def test_the_kamla_link_uses_the_bots_own_role_and_never_creates_a_second_one(env, native):
    g = LinkGuild()
    before = [r.name for r in g.roles]
    rows = await lk.refresh_links(g)
    kamla = next(r for r in rows if r["key"] == "kamla")
    assert kamla["role"] is g.self_role                                   # the existing role, no new one
    assert str(g.self_role.id) in {p["role_ids"][0] for p in native}      # it was attempted with the real role id
    assert [r.name for r in g.roles] == before and not any(c[0] == "role" for c in g.create_calls)
    # Discord refused a managed role: said honestly, the link is a plain invite
    assert kamla["native"] is False
    assert "without granting a role" in g.links.messages[0].content


async def test_links_are_posted_in_the_channel_without_previews_and_never_by_dm(env, native):
    g = LinkGuild()
    await lk.refresh_links(g)
    assert len(g.links.messages) == 1
    msg = g.links.messages[0]
    assert msg.kw["suppress_embeds"] is True                              # no link previews
    assert msg.kw["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()   # no pings from the labels
    for label in ("ORG", "CAP", "TABBY", "EQUITY", "INVITED ADJUDICATOR", "INDEPENDENT ADJUDICATOR", "DEBATER",
                  "VISITOR", "KAMLA"):
        assert f"**{label}**" in msg.content
    assert msg.content.count("https://discord.gg/") == 9                  # each link exactly once
    assert msg.embed is None


async def test_rerun_edits_the_same_message_and_reuses_invites_no_duplicates(env, native):
    g = LinkGuild()
    first = await lk.refresh_links(g)
    invites_before, calls = len(g.live), len(native)
    again = await lk.refresh_links(g)
    assert [r["code"] for r in again] == [r["code"] for r in first]
    assert len(g.live) == invites_before and len(native) == calls          # nothing created again
    assert len(g.links.messages) == 1 and g.links.messages[0].edits == []   # identical text -> not even an edit


async def test_changed_or_deleted_role_updates_in_place(env, native):
    g = LinkGuild()
    first = await lk.refresh_links(g)
    org = next(r for r in g.roles if r.name == "ORG")
    g.roles.remove(org)                                                   # role deleted
    g.roles.append(FakeRole("ORG", position=3))                          # re-created: new id
    rows = await lk.refresh_links(g)
    new_org = next(r for r in rows if r["key"] == "org")
    old_org = next(r for r in first if r["key"] == "org")
    assert new_org["code"] != old_org["code"] and g.live[old_org["code"]].deleted     # only OUR old invite retired
    assert len(g.links.messages) == 1 and g.links.messages[0].edits           # edited, not re-posted
    assert g.links.messages[0].content.count("https://discord.gg/") == 9


async def test_a_missing_role_is_labelled_not_invented(env, native):
    g = LinkGuild()
    g.roles = [r for r in g.roles if r.name != "EQUITY"]
    rows = await lk.refresh_links(g)
    assert next(r for r in rows if r["key"] == "equity")["available"] is False
    assert "role not available yet" in g.links.messages[0].content
    assert not any(c[0] == "role" for c in g.create_calls)


async def test_revoked_link_is_replaced(env, native):
    g = LinkGuild()
    first = await lk.refresh_links(g)
    del g.live[first[2]["code"]]                                          # someone revoked the TABBY link
    again = await lk.refresh_links(g)
    assert again[2]["code"] != first[2]["code"] and again[0]["code"] == first[0]["code"]


async def test_fallback_to_plain_invites_when_discord_rejects_role_ids(env, monkeypatch):
    async def always_refuse(channel, payload, reason):
        raise http_error(403, code=50013)
    monkeypatch.setattr(lk, "_http_create_invite", always_refuse)
    g = LinkGuild()
    rows = await lk.refresh_links(g)
    assert all(not r["native"] for r in rows) and all(r["available"] for r in rows)
    assert "assigned by KAMLA when the member joins" in g.links.messages[0].content
    import invite_roles_cog as ir
    assert g.id in ir._tracked                                            # the use-count fallback is armed


async def test_long_content_is_split_never_truncated(env, native):
    rows = [{"key": f"k{i}", "label": "X" * 40, "role": object(), "available": True,
             "native": True, "url": "https://discord.gg/" + "a" * 20} for i in range(60)]
    texts = lk.render_links(rows)
    assert len(texts) > 1 and all(len(t) <= 2000 for t in texts)
    assert sum(t.count("https://discord.gg/") for t in texts) == 60


async def test_a_lost_config_does_not_duplicate_the_message(env, native):
    g = LinkGuild()
    await lk.refresh_links(g)
    env.data[g.id].pop("links_message_ids")                               # config forgot the message id
    await lk.refresh_links(g)
    assert len([m for m in g.links.messages if not m.deleted]) == 1


async def test_links_channel_is_staff_only_and_read_only():
    import server_builder as sb
    g = FakeGuild()
    roles = {n: FakeRole(n) for n in cfgmod.KAMLA_ROLE_NAMES}
    ow = sb._build_links_overwrites(g, roles)
    assert ow[g.default_role].view_channel is False and ow[g.default_role].send_messages is False
    assert ow[roles["ORG"]].view_channel is True and ow[roles["ORG"]].send_messages is False
    assert roles["DEBATER"] not in ow and roles["VISITOR"] not in ow


def test_no_code_path_sends_a_direct_message():
    """KAMLA must never DM anyone: scan every module for the ways discord.py sends one."""
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parent.parent
    pattern = re.compile(r"create_dm\(|dm_channel|(?:user|member|author|owner|installer|creator|m|u)\.send\(|"
                         r"interaction\.user\.send\(")
    offenders = []
    for path in root.glob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not line.lstrip().startswith("#"):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == []

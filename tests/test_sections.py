"""Developer section and Get Role panel as posted by the builder."""
import discord
import pytest

import naming
import roles_cog
import server_builder as sb
from ratelimit import RequestCoordinator
from tests.fakes import FakeChannel, FakeGuild


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    async def nosleep(_):
        return None
    monkeypatch.setattr(sb, "bulk", RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep, "rng": lambda: 0.0}))


def test_developer_buttons_support_server_replaces_get_in_touch():
    view = sb.developer_view()
    labels = {b.label: b for b in view.children}
    assert "Get In Touch" not in labels and not any("touch" in (l or "").lower() for l in labels)
    support = labels["JOIN KAMLA Support Server"]
    assert support.url == "https://discord.gg/9zhXq4MNHs" and support.style == discord.ButtonStyle.link
    assert "Get In Touch" not in open(sb.__file__, encoding="utf-8").read()


async def test_developer_channel_message_carries_the_support_button():
    g = FakeGuild()
    ch = FakeChannel(g, naming.CH_DEVELOPERS)
    await sb._post_meet_developer(ch)
    msg = ch.messages[0]
    assert msg.embed.title == "👨‍💻 Meet Our Developers"
    assert "JOIN KAMLA Support Server" in [b.label for b in msg.view.children]


async def test_get_role_panel_is_posted_with_the_select_menu():
    g = FakeGuild()
    ch = FakeChannel(g, naming.CH_GET_ROLE)
    g._channels[ch.id] = ch
    await sb._post_get_role(g)
    msg = ch.messages[0]
    assert isinstance(msg.view, roles_cog.RoleSelectView)
    assert "Get Your Role" in msg.embed.title
    text = "\n".join(f.value for f in msg.embed.fields)
    for label in ("Invited Adjudicator", "Independent Adjudicator", "Debater", "Visitor"):
        assert label in text                                  # a clear description for every role
    assert not any(r.name == "KAMLA" for r in g.roles)

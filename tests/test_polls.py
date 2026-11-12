import types
from unittest import mock

import discord
import pytest

import polls_cog as pc


@pytest.fixture(autouse=True)
def clean():
    pc._active_polls.clear()
    yield
    pc._active_polls.clear()


def make_poll(n_options=2, anonymous=False, topic="Best motion?", options=None):
    return pc.Poll(guild_id=1, channel_id=2, creator_id=3, topic=topic,
                   options=options or [f"Option {i}" for i in range(1, n_options + 1)],
                   multiple_votes=False, anonymous=anonymous, is_yesno=False, role_ids=set(), user_ids=set())


def test_embed_stays_within_discords_limits_with_hundreds_of_public_voters():
    poll = make_poll(n_options=9)
    for idx in range(9):
        poll.votes[idx] = {10**17 + idx * 1000 + k for k in range(300)}          # realistic 18-digit user ids
    embed = pc._build_poll_embed(poll)
    assert all(len(f.value) <= 1024 and len(f.name) <= 256 for f in embed.fields)
    assert len(embed) <= 6000                                                     # Discord rejects anything bigger
    assert any("more" in f.value for f in embed.fields)                           # the list says it was cut
    assert "300 vote" in embed.fields[0].name                                    # but the count is always exact


def test_voters_text_small_lists_are_complete_and_sorted():
    assert pc._voters_text(set(), 100) == "None"
    assert pc._voters_text({30, 10, 20}, 1000) == "<@10>\n<@20>\n<@30>"


def test_voters_text_never_exceeds_its_budget():
    for n in (1, 5, 47, 48, 500):
        ids = {10**17 + k for k in range(n)}
        assert len(pc._voters_text(ids, 1000)) <= 1000
        assert len(pc._voters_text(ids, 60)) <= 60


def test_an_overlong_topic_cannot_break_the_embed():
    embed = pc._build_poll_embed(make_poll(topic="x" * 900))
    assert len(embed.title) <= 256


def admin_interaction(channel):
    i = mock.MagicMock()
    i.guild = types.SimpleNamespace(id=1, get_role=lambda r: None, get_member=lambda u: None)
    i.channel = channel
    i.user = types.SimpleNamespace(id=3, roles=[], guild_permissions=types.SimpleNamespace(administrator=True))
    i.response.send_message = mock.AsyncMock()
    i.response.defer = mock.AsyncMock()
    i.followup.send = mock.AsyncMock()
    return i


def text_channel(name, send=None):
    ch = mock.MagicMock(spec=discord.TextChannel)
    ch.name, ch.id, ch.mention = name, 2, "#c"
    ch.send = send or mock.AsyncMock(return_value=types.SimpleNamespace(id=99))
    return ch


async def test_too_long_topic_or_option_is_refused_before_anything_is_created():
    cog = pc.PollsCog(bot=None)
    ch = text_channel(pc.POLL_CHANNEL_NAME)
    i = admin_interaction(ch)
    await cog.newpoll.callback(cog, i, "x" * 300, "a,b")
    assert "too long" in i.response.send_message.await_args.args[0]
    i = admin_interaction(ch)
    await cog.newpoll.callback(cog, i, "ok", "a," + "b" * 150)
    assert "at most 100 characters" in i.response.send_message.await_args.args[0]
    assert not pc._active_polls and ch.send.await_count == 0


async def test_yes_no_vote_is_created_and_confirmed_after_posting():
    cog = pc.PollsCog(bot=None)
    ch = text_channel(pc.YESNO_CHANNEL_NAME)
    i = admin_interaction(ch)
    await cog.newpoll.callback(cog, i, "Adopt the rule?", None)
    assert ch.send.await_count == 1 and len(pc._active_polls) == 1
    assert "Yes/No vote created" in i.followup.send.await_args.args[0]


async def test_poll_that_cannot_be_posted_is_not_kept_and_the_creator_is_told():
    cog = pc.PollsCog(bot=None)
    ch = text_channel(pc.YESNO_CHANNEL_NAME, send=mock.AsyncMock(side_effect=RuntimeError("Missing Permissions")))
    i = admin_interaction(ch)
    await cog.newpoll.callback(cog, i, "Adopt the rule?", None)
    assert not pc._active_polls
    assert "could not post" in i.followup.send.await_args.args[0]

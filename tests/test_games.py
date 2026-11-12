import types
from unittest import mock

import pytest

import games_cog as gc


@pytest.fixture(autouse=True)
def clean():
    gc._busy.clear()
    yield
    gc._busy.clear()


def member(uid, bot=False):
    return types.SimpleNamespace(id=uid, bot=bot, mention=f"<@{uid}>", display_name=f"u{uid}",
                                 display_avatar=types.SimpleNamespace(url="http://x/a.png"))


class BrokenChannel:
    async def send(self, *a, **k):
        raise RuntimeError("Missing Permissions")


@pytest.mark.parametrize("handler,args", [
    ("_handle_ttt_challenge_from_message", ()),
    ("_handle_rps_challenge_from_message", (3,)),
])
async def test_failed_challenge_post_does_not_leave_players_busy_from_a_message(handler, args):
    a, b = member(1), member(2)
    message = types.SimpleNamespace(guild=types.SimpleNamespace(id=9), author=a, channel=BrokenChannel())
    with pytest.raises(RuntimeError):
        await getattr(gc, handler)(message, b, *args)
    assert not gc._is_busy(9, 1) and not gc._is_busy(9, 2)


@pytest.mark.parametrize("handler,args", [
    ("_handle_ttt_challenge", ()),
    ("_handle_rps_challenge", (2,)),
])
async def test_failed_challenge_post_does_not_leave_players_busy_from_a_slash_command(handler, args):
    a, b = member(1), member(2)
    interaction = mock.MagicMock()
    interaction.guild, interaction.user, interaction.channel = types.SimpleNamespace(id=9), a, BrokenChannel()
    interaction.response.send_message = mock.AsyncMock()
    with pytest.raises(RuntimeError):
        await getattr(gc, handler)(interaction, b, *args)
    assert not gc._is_busy(9, 1) and not gc._is_busy(9, 2)


async def test_successful_challenge_keeps_both_players_busy_until_it_ends():
    a, b = member(1), member(2)

    class Chan:
        async def send(self, *a, **k):
            return types.SimpleNamespace()
    message = types.SimpleNamespace(guild=types.SimpleNamespace(id=9), author=a, channel=Chan())
    await gc._handle_ttt_challenge_from_message(message, b)
    assert gc._is_busy(9, 1) and gc._is_busy(9, 2)

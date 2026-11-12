import types
from unittest import mock

import pytest

import dm_notify_cog as dn
from tests.fakes import http_error


def member_leaving(audit_error=None, entries=()):
    async def audit_logs(limit=5):
        if audit_error is not None:
            raise audit_error
        for e in entries:
            yield e
    guild = types.SimpleNamespace(name="G", icon=None, audit_logs=audit_logs)
    return types.SimpleNamespace(id=5, guild=guild, send=mock.AsyncMock())


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_dm(user, embed):
        calls.append(embed.title)
    monkeypatch.setattr(dn, "_dm", fake_dm)
    return calls


async def test_unknown_guild_404_is_not_a_crash_and_sends_no_dm(sent):
    cog = dn.DMNotifyCog(bot=None)
    await cog.on_member_remove(member_leaving(http_error(404, code=10004, text="Unknown Guild")))
    assert sent == []                                                  # KAMLA is not in that server any more


async def test_missing_audit_permission_still_sends_the_normal_leave_message(sent):
    cog = dn.DMNotifyCog(bot=None)
    await cog.on_member_remove(member_leaving(http_error(403)))
    assert len(sent) == 1


async def test_other_discord_errors_also_fall_back_to_the_leave_message(sent):
    cog = dn.DMNotifyCog(bot=None)
    await cog.on_member_remove(member_leaving(http_error(500)))
    assert len(sent) == 1

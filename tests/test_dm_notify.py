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


def entry(action, target_id):
    return types.SimpleNamespace(action=action, target=types.SimpleNamespace(id=target_id))


async def fast_sleep(_):
    return None


async def test_find_removal_action_recovers_from_audit_log_lag():
    """The kick isn't visible yet on the first read (Discord's own documented audit-log delay) — the
    SAME situation already handled for channel-creation elsewhere in this codebase."""
    calls = {"n": 0}

    async def audit_logs(limit=10):
        calls["n"] += 1
        if calls["n"] < 2:
            return
        yield entry(__import__("discord").AuditLogAction.kick, 5)
    guild = types.SimpleNamespace(audit_logs=audit_logs)
    assert await dn._find_removal_action(guild, 5, sleep=fast_sleep) == "kicked"
    assert calls["n"] == 2


async def test_find_removal_action_keeps_looking_past_an_unrelated_entry():
    """An earlier bug stopped at the FIRST entry mentioning this member, even when it wasn't a kick or
    ban (e.g. a role edit) — missing a genuine kick/ban entry further down the list."""
    import discord as d

    async def audit_logs(limit=10):
        yield entry(d.AuditLogAction.member_role_update, 5)      # unrelated entry, same member
        yield entry(d.AuditLogAction.kick, 5)                    # the real kick, one entry further back
    guild = types.SimpleNamespace(audit_logs=audit_logs)
    assert await dn._find_removal_action(guild, 5, sleep=fast_sleep) == "kicked"


async def test_find_removal_action_gives_up_as_left_after_retries_exhausted():
    async def audit_logs(limit=10):
        return
        yield  # pragma: no cover - makes this an async generator with nothing to yield
    guild = types.SimpleNamespace(audit_logs=audit_logs)
    assert await dn._find_removal_action(guild, 5, sleep=fast_sleep) == "left"


# ── developer-invite joins get no "Welcome!" DM either ──────────────────────────

async def test_join_dm_is_skipped_for_the_developer_invite(monkeypatch, sent):
    import importlib
    ir = importlib.import_module("invite_roles_cog")
    monkeypatch.setattr(ir, "join_outcome", mock.AsyncMock(return_value="dev"))
    cog = dn.DMNotifyCog(bot=None)
    member = types.SimpleNamespace(id=1, guild=types.SimpleNamespace(id=1), bot=False, mention="<@1>", send=mock.AsyncMock())
    await cog.on_member_join(member)
    assert sent == []


async def test_join_dm_still_sent_for_an_ordinary_join(monkeypatch, sent):
    import importlib
    ir = importlib.import_module("invite_roles_cog")
    monkeypatch.setattr(ir, "join_outcome", mock.AsyncMock(return_value="visitor"))
    cog = dn.DMNotifyCog(bot=None)
    member = types.SimpleNamespace(id=1, guild=types.SimpleNamespace(id=1, name="G", icon=None), bot=False,
                                   mention="<@1>", send=mock.AsyncMock())
    await cog.on_member_join(member)
    assert len(sent) == 1

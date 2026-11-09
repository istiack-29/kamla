import types
from unittest import mock

import discord

import setup_cog
import server_builder as sb
import importlib
from tests.fakes import http_error, FakeGuild


class Gone(Exception):
    pass


async def test_build_report_survives_deleted_command_channel(monkeypatch):
    """The wipe deletes the channel /st was used in. The final report must fall back to a DM and the
    build must NOT be reported as failed (this is exactly what the live log showed)."""
    guild = FakeGuild()
    guild.name = "g"
    report = sb.BuildReport()
    report.created.append("x")

    async def noop(*a, **k):
        return None

    async def fake_build(**k):
        return report

    class Job:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(sb, "building_job", lambda g: Job())
    monkeypatch.setattr(sb, "find_resumable", mock.AsyncMock(return_value=False))
    monkeypatch.setattr(sb, "wipe_server", noop)
    monkeypatch.setattr(sb, "mark_build_state", noop)
    monkeypatch.setattr(sb, "build_server", fake_build)
    monkeypatch.setattr(setup_cog, "_assign_creator_role", noop)
    monkeypatch.setattr(setup_cog.config_manager, "get_config", mock.AsyncMock(return_value={}))
    import webhook
    monkeypatch.setattr(webhook, "edit_join_log", noop)
    links = [{"key": "visitor", "label": "[V] Visitor", "roles": ["VISITOR"], "code": "c", "url": "https://discord.gg/c"}]
    # discord.py removes unloaded extension modules from sys.modules, so earlier tests may have left
    # a stale module object behind; patch whichever module _do_build will actually import.
    ir = importlib.import_module("invite_roles_cog")
    monkeypatch.setattr(ir, "create_role_invites", mock.AsyncMock(return_value=links))

    status_msg = mock.MagicMock()
    status_msg.edit = mock.AsyncMock(side_effect=http_error(404, code=10008))       # Unknown Message
    dm_calls = []

    async def dm(*a, **k):
        dm_calls.append(k.get("embed") or (a[0] if a else None))

    user = types.SimpleNamespace(id=1, send=dm)
    interaction = mock.MagicMock(guild=guild, user=user)
    interaction.followup.send = mock.AsyncMock(side_effect=[status_msg, http_error(400, code=10003)])

    data = {"format": "ap", "rooms": 1, "timezone": "+06:00", "tournament_name": "T", "role_name": "ORG"}
    await setup_cog._do_build(interaction, data)            # must not raise

    assert interaction.followup.send.await_count == 1        # no attempt to use the dead channel again
    titles = [d.title or "" for d in dm_calls if isinstance(d, discord.Embed)]
    assert any("Ready" in t for t in titles)                 # the success report arrived by DM
    assert not any("Failed" in t for t in titles)
    assert any(isinstance(d, str) and "discord.gg/c" in d for d in dm_calls)    # invite links DM

"""matchup / motion / ballot / break: nobody can delete or rename them, and KAMLA brings them back."""
import types

import discord
import pytest

import server_builder as sb
from ratelimit import RequestCoordinator
from tests.fakes import FakeConfig, FakeGuild


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    async def nosleep(_):
        return None
    monkeypatch.setattr(sb, "bulk", RequestCoordinator("t", 2, 0, retry_kwargs={"sleep": nosleep, "rng": lambda: 0.0}))
    monkeypatch.setattr(sb, "config_manager", FakeConfig())
    monkeypatch.setattr(sb.asyncio, "sleep", nosleep)

    async def noop(*a, **k):
        return None
    for name in ("_post_meet_developer", "_post_welcome", "_post_get_role", "_post_how_to_use",
                 "_post_settings_panel", "_post_all_in_panel"):
        monkeypatch.setattr(sb, name, noop)


async def built():
    g = FakeGuild()
    await sb.build_server(g, "ap", 1, "+06:00", "T", 1)
    return g


def tab(g, name):
    cat = next(c for c in g.categories if c.name == sb.GRAND_AUDITORIUM)
    return next((c for c in cat.text_channels if c.name == name), None)


def test_the_four_channels_are_the_ones_the_commands_post_to():
    import tabbycat_cog
    assert set(sb.TAB_CHANNELS) == set(tabbycat_cog.CHANNELS.values())
    assert sb.GRAND_AUDITORIUM in sb.PROTECTED_CATEGORIES


async def test_a_fresh_server_already_denies_manage_channels_to_everyone():
    g = await built()
    for name in sb.TAB_CHANNELS:
        ow = tab(g, name).overwrites[g.default_role]
        assert ow.manage_channels is False                              # no role can delete/rename, even if granted later
        assert tab(g, name).overwrites[g.me].manage_channels is True    # KAMLA itself still can (slowmode, rename-back)


async def test_a_deleted_channel_comes_back_in_the_same_category():
    g = await built()
    await tab(g, "🎓︱motion").delete()
    assert tab(g, "🎓︱motion") is None
    assert await sb.protect_tab_channels(g) == 1
    back = tab(g, "🎓︱motion")
    assert back is not None and back.overwrites[g.default_role].manage_channels is False
    assert await sb.protect_tab_channels(g) == 0                        # nothing more to do


async def test_all_four_deleted_while_kamla_was_offline_are_restored_at_once():
    g = await built()
    for name in sb.TAB_CHANNELS:
        await tab(g, name).delete()
    assert await sb.protect_tab_channels(g) == 4
    assert all(tab(g, n) is not None for n in sb.TAB_CHANNELS)


async def test_whole_category_deleted_is_restored_by_the_delete_guard_and_then_checked():
    g = await built()
    cat = next(c for c in g.categories if c.name == sb.GRAND_AUDITORIUM)
    for ch in list(cat.channels):
        await ch.delete()
    await cat.delete()
    assert await sb.restore_if_protected(g, sb.GRAND_AUDITORIUM, True) is True
    assert all(tab(g, n) is not None for n in sb.TAB_CHANNELS)


async def test_old_servers_get_the_protection_added_once():
    g = await built()
    ch = tab(g, "🎓︱ballot")
    ch.overwrites[g.default_role] = discord.PermissionOverwrite(view_channel=False)     # how it was before this rule
    ch.overwrites.pop(g.me)
    before = g.edits
    await sb.protect_tab_channels(g)
    assert ch.overwrites[g.default_role].manage_channels is False and ch.overwrites[g.me].manage_channels is True
    assert g.edits > before
    again = g.edits
    await sb.protect_tab_channels(g)
    assert g.edits == again                                             # already right → no request at all


async def test_servers_not_built_by_kamla_are_left_alone():
    g = FakeGuild()
    await g.create_text_channel("general")
    created = len(g.create_calls)
    assert await sb.protect_tab_channels(g) == 0 and len(g.create_calls) == created


async def test_nothing_is_restored_while_the_server_is_being_built():
    g = await built()
    await tab(g, "🎓︱break").delete()
    sb._building_guilds.add(g.id)
    try:
        assert await sb.protect_tab_channels(g) == 0
    finally:
        sb._building_guilds.discard(g.id)


# ── rename guard ──────────────────────────────────────────────────────────────

async def _bot(monkeypatch):
    import main
    bot = main.KamlaBot()
    monkeypatch.setattr(type(bot), "user", types.SimpleNamespace(id=555), raising=False)
    return bot


async def test_renaming_a_protected_channel_is_undone(monkeypatch):
    g = await built()
    bot = await _bot(monkeypatch)
    ch = tab(g, "🎓︱motion")
    before = types.SimpleNamespace(name="🎓︱motion", guild=g)
    await ch.edit(name="oops")                                          # someone renamed it
    await bot.on_guild_channel_update(before, ch)
    assert ch.name == "🎓︱motion"
    await bot.close()


async def test_other_renames_and_the_revert_itself_are_ignored(monkeypatch):
    g = await built()
    bot = await _bot(monkeypatch)
    other = next(c for c in g.text_channels if c.name == "📨︱ga-text")
    edits = g.edits
    await other.edit(name="renamed")
    await bot.on_guild_channel_update(types.SimpleNamespace(name="📨︱ga-text", guild=g), other)
    assert other.name == "renamed"                                      # not a tournament channel → not our business
    ch = tab(g, "🎓︱motion")
    await bot.on_guild_channel_update(types.SimpleNamespace(name="oops", guild=g), ch)     # the revert's own event
    assert ch.name == "🎓︱motion"
    await bot.close()

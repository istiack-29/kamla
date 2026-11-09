import types

import welcome_cog


def member(count=3, banner=None, icon=None):
    guild = types.SimpleNamespace(
        name="Kijani", member_count=count, banner=banner, icon=icon,
        me=types.SimpleNamespace(display_avatar=types.SimpleNamespace(url="https://x/me.png")))
    return types.SimpleNamespace(
        mention="<@1>", guild=guild, display_avatar=types.SimpleNamespace(url="https://x/a.png"))


def test_welcome_embed_is_short():
    e = welcome_cog._build_welcome_embed(member())
    assert e.title.startswith("Welcome to Kijani")
    assert "<@1>" in e.description and "3rd member" in e.description
    assert len(e.fields) == 0                                   # no Quick Start / Roles / Key Channels blocks
    assert "KAMLA" not in e.description and len(e.description) < 120
    assert e.thumbnail.url == "https://x/a.png"


def test_ordinals():
    assert [welcome_cog._ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 112)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "112th"]

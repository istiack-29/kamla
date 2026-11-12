import itertools
import json
import types

import pytest

import config as cfgmod

ME = types.SimpleNamespace(id=555)
_gid = itertools.count(7000)


class Att:
    def __init__(self, filename, data):
        self.filename, self.data = filename, data

    async def read(self):
        return self.data


class Msg:
    def __init__(self, author, content="", attachments=()):
        self.author, self.content, self.attachments = author, content, list(attachments)
        self.created_at = None


class Chan:
    def __init__(self, name, visible=False, fail_send=False):
        self.id = next(_gid)
        self.name, self.visible, self.fail_send = name, visible, fail_send
        self.msgs: list[Msg] = []

    def permissions_for(self, role):
        return types.SimpleNamespace(view_channel=self.visible)

    async def send(self, content=None, file=None, **kw):
        if self.fail_send:
            raise RuntimeError("cannot send")
        atts = []
        if file is not None:
            atts.append(Att(file.filename, file.fp.read()))
        self.msgs.append(Msg(ME, content or "", atts))

    async def history(self, limit=100, oldest_first=False):
        for m in reversed(self.msgs):
            yield m


class Guild:
    def __init__(self, *channels):
        self.id = next(_gid)
        self.me = ME
        self.default_role = object()
        self.text_channels = list(channels)


@pytest.fixture(autouse=True)
def clean():
    cfgmod.ConfigManager._cache.clear()
    cfgmod.ConfigManager._channel_ids.clear()
    cfgmod.ConfigManager._miss_until.clear()


def fresh():
    return cfgmod.ConfigManager()           # (the caches are shared class attributes → cleared here = "restart")


def restart():
    cfgmod.ConfigManager._cache.clear()
    cfgmod.ConfigManager._miss_until.clear()


async def test_small_config_is_one_inline_message_and_survives_a_restart():
    ch = Chan(cfgmod.CHANNEL_NAME)
    g = Guild(ch)
    assert await fresh().set_config(g, {"locked": True}) is True
    assert [m.content for m in ch.msgs] == ['{"locked": true}'] and not ch.msgs[0].attachments
    restart()
    assert await fresh().get_config(g) == {"locked": True}


async def test_large_config_is_saved_as_a_file_instead_of_being_dropped():
    ch = Chan(cfgmod.CHANNEL_NAME)
    g = Guild(ch)
    big = {"tabbycat": {"url": "https://x.example", "slug": "t", "token": "secret"},
           "ids": list(range(1, 500)), "name": "বাংলা ✓"}
    assert len(json.dumps(big, ensure_ascii=False)) > cfgmod.MAX_INLINE
    assert await fresh().set_config(g, big) is True
    assert ch.msgs[-1].attachments and ch.msgs[-1].attachments[0].filename == cfgmod.CONFIG_FILE
    restart()
    assert await fresh().get_config(g) == big                           # read back from the attachment, Bengali intact


async def test_config_too_large_for_even_a_file_reports_failure_but_keeps_it_in_memory():
    ch = Chan(cfgmod.CHANNEL_NAME)
    g = Guild(ch)
    huge = {"blob": "x" * (cfgmod.MAX_FILE_BYTES + 10)}
    m = fresh()
    assert await m.set_config(g, huge) is False
    assert m.get_cached(g.id) == huge and ch.msgs == []


async def test_save_failure_is_reported_not_hidden():
    ch = Chan(cfgmod.CHANNEL_NAME, fail_send=True)
    g = Guild(ch)
    m = fresh()
    assert await m.set_config(g, {"a": 1}) is False
    assert m.get_cached(g.id) == {"a": 1}                               # still usable until the restart


async def test_only_kamlas_own_messages_are_trusted():
    ch = Chan(cfgmod.CHANNEL_NAME)
    other_bot = types.SimpleNamespace(id=999, bot=True)
    ch.msgs.append(Msg(other_bot, '{"locked": false, "tabbycat": {"token": "evil"}}'))
    ch.msgs.append(Msg(other_bot, "", [Att(cfgmod.CONFIG_FILE, b'{"x": 1}')]))
    g = Guild(ch)
    assert await fresh().get_config(g) == {}                            # an impostor bot cannot inject settings
    ch.msgs.insert(0, Msg(ME, '{"real": true}'))
    restart()
    assert await fresh().get_config(g) == {"real": True}


def test_is_own_config_rules():
    mine, other = types.SimpleNamespace(id=555), types.SimpleNamespace(id=1)
    assert cfgmod.is_own_config(Msg(mine, '{"a": 1}'), 555)
    assert cfgmod.is_own_config(Msg(mine, "x", [Att(cfgmod.CONFIG_FILE, b"{}")]), 555)
    assert not cfgmod.is_own_config(Msg(mine, "hello"), 555)
    assert not cfgmod.is_own_config(Msg(mine, "x", [Att("other.json", b"{}")]), 555)
    assert not cfgmod.is_own_config(Msg(other, '{"a": 1}'), 555)


def test_a_visible_lookalike_channel_is_never_used_for_the_config():
    lookalike = Chan("📢-kamla-config-public", visible=True)
    real = Chan(cfgmod.CHANNEL_NAME, visible=False)
    assert cfgmod.find_config_channel(Guild(lookalike)) is None
    assert cfgmod.find_config_channel(Guild(lookalike, real)) is real
    renamed = Chan("old-kamla-config", visible=False)
    assert cfgmod.find_config_channel(Guild(renamed)) is renamed        # a hidden renamed one still works
    exact = Chan(cfgmod.CHANNEL_NAME, visible=False)
    assert cfgmod.find_config_channel(Guild(renamed, exact)) is exact   # the exact name wins


async def test_compact_keeps_only_the_newest_copy_including_file_copies():
    ch = Chan(cfgmod.CHANNEL_NAME)
    deleted = []
    for n in range(3):
        m = Msg(ME, f'{{"v": {n}}}')
        m.created_at = cfgmod.discord.utils.utcnow()
        async def _del(m=m):
            deleted.append(m.content)
        m.delete = _del
        ch.msgs.append(m)
    big = Msg(ME, "📦", [Att(cfgmod.CONFIG_FILE, b'{"v": 9}')])
    big.created_at = cfgmod.discord.utils.utcnow()
    async def _delbig():
        deleted.append("file")
    big.delete = _delbig
    ch.msgs.insert(0, big)                                              # oldest: a file copy
    g = Guild(ch)

    async def bulk_delete(batch):
        for m in batch:
            await m.delete()
    ch.delete_messages = bulk_delete
    await fresh().compact(g)
    assert sorted(deleted) == ['file', '{"v": 0}', '{"v": 1}']           # the newest ({"v": 2}) stays

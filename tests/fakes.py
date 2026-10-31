"""Minimal in-memory stand-ins for the parts of discord.py that KAMLA's builder touches."""
import itertools
import types

import discord

_ids = itertools.count(1000)


def http_error(status: int, *, via: bool = True, retry_after: float | None = None, code: int = 0, text: str = "err"):
    headers = {}
    if via:
        headers["Via"] = "1.1 google"
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    resp = types.SimpleNamespace(status=status, reason="x", headers=headers)
    cls = {403: discord.Forbidden, 404: discord.NotFound}.get(status, discord.HTTPException)
    return cls(resp, {"message": text, "code": code})


class FakeMessage:
    def __init__(self, author, content="", embed=None, view=None):
        self.id = next(_ids)
        self.author = author
        self.content = content
        self.embed = embed
        self.view = view
        self.mentions = []
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeChannel:
    kind = "text"

    def __init__(self, guild, name, category=None, overwrites=None, user_limit=0):
        self.id = next(_ids)
        self.guild = guild
        self.name = name
        self.category = category
        self.category_id = category.id if category else None
        self.overwrites = dict(overwrites or {})
        self.user_limit = user_limit
        self.position = len(guild._channels)
        self.messages: list[FakeMessage] = []
        self.history_calls = 0
        self.members = []

    async def send(self, content=None, **kw):
        msg = FakeMessage(self.guild.me, content or "", kw.get("embed"), kw.get("view"))
        self.messages.append(msg)
        self.guild.sent += 1
        return msg

    async def history(self, limit=100, **kw):  # used as: async for m in ch.history(...)
        self.history_calls += 1
        for m in list(reversed(self.messages))[:limit]:
            yield m

    async def edit(self, **kw):
        if "overwrites" in kw:
            self.overwrites = dict(kw["overwrites"])
        if "user_limit" in kw:
            self.user_limit = kw["user_limit"]
        self.guild.edits += 1

    async def delete(self, reason=None):
        self.guild._channels.pop(self.id, None)
        self.guild.deleted += 1


class FakeVoice(FakeChannel):
    kind = "voice"


class FakeCategory(FakeChannel):
    kind = "category"

    @property
    def text_channels(self):
        return [c for c in self.guild._channels.values() if c.kind == "text" and c.category_id == self.id]

    @property
    def voice_channels(self):
        return [c for c in self.guild._channels.values() if c.kind == "voice" and c.category_id == self.id]

    @property
    def channels(self):
        return [c for c in self.guild._channels.values() if c.category_id == self.id]


class FakeRole:
    def __init__(self, name, position=1, managed=False):
        self.id = next(_ids)
        self.name = name
        self.position = position
        self.managed = managed

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, o):
        return isinstance(o, FakeRole) and o.id == self.id

    async def delete(self, reason=None):
        pass


class FakeMe:
    def __init__(self, edit):
        self.id = next(_ids)
        self.roles = []
        self.edit = edit

    def __hash__(self):
        return hash(self.id)


class FakeGuild:
    """Records every 'API call'. `fail` maps a resource name to a list of exceptions to raise
    (consumed one per attempt) so tests can inject transient / permanent failures."""

    def __init__(self):
        self.id = next(_ids)
        self.name = "g"
        self._channels: dict[int, FakeChannel] = {}
        self.default_role = FakeRole("@everyone", 0)
        self.me = FakeMe(self._noop)
        self.roles = [self.default_role]
        self.fail: dict[str, list] = {}
        self.create_calls: list[tuple[str, str]] = []
        self.sent = 0
        self.edits = 0
        self.deleted = 0
        self.icon = None

    async def _noop(self, **kw):
        return None

    async def edit(self, **kw):
        return None

    # --- views -------------------------------------------------------------
    @property
    def channels(self):
        return list(self._channels.values())

    @property
    def categories(self):
        return [c for c in self._channels.values() if c.kind == "category"]

    @property
    def text_channels(self):
        return [c for c in self._channels.values() if c.kind == "text"]

    @property
    def voice_channels(self):
        return [c for c in self._channels.values() if c.kind == "voice"]

    # --- API ---------------------------------------------------------------
    def _maybe_fail(self, name):
        queue = self.fail.get(name)
        if queue:
            exc = queue.pop(0)
            if exc is not None:
                raise exc

    async def _create(self, cls, kind, name, **kw):
        self.create_calls.append((kind, name))
        self._maybe_fail(name)
        ch = cls(self, name, kw.get("category"), kw.get("overwrites"), kw.get("user_limit", 0))
        self._channels[ch.id] = ch
        return ch

    async def create_text_channel(self, name, **kw):
        return await self._create(FakeChannel, "text", name, **kw)

    async def create_voice_channel(self, name, **kw):
        return await self._create(FakeVoice, "voice", name, **kw)

    async def create_category(self, name, **kw):
        return await self._create(FakeCategory, "category", name, **kw)

    async def create_role(self, name, **kw):
        self.create_calls.append(("role", name))
        self._maybe_fail(name)
        r = FakeRole(name, len(self.roles))
        self.roles.append(r)
        return r


class FakeConfig:
    """In-memory replacement for config_manager."""

    def __init__(self):
        self.data: dict[int, dict] = {}

    def get_cached(self, gid):
        return dict(self.data.get(gid, {}))

    async def get_config(self, guild):
        return dict(self.data.get(guild.id, {}))

    async def set_config(self, guild, d):
        self.data[guild.id] = dict(d)

    async def update_config(self, guild, **kw):
        cur = dict(self.data.get(guild.id, {}))
        cur.update(kw)
        self.data[guild.id] = cur
        return dict(cur)

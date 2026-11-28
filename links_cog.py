"""links_cog.py — permanent role-invitation links, posted in 🔗︱links (never by DM).

Discord's "Community Invites" feature lets an invite carry `role_ids`: whoever accepts it receives those roles, with
no bot involved. discord.py 2.7 does not wrap the parameter yet, so the request is made on the library's HTTP client.

Rules Discord enforces (docs.discord.com → Community Invites): the bot needs Create Instant Invite + Manage Roles and
can only attach roles that are LOWER than its own highest role. KAMLA's own (managed, integration) role is its highest
role and cannot be given to members, so Discord is expected to refuse it. KAMLA tries anyway; if Discord refuses it
posts a plain permanent invite and the links message says so — nothing is claimed that did not happen.

If Discord rejects `role_ids` for a tournament role too (missing permission, feature unavailable) KAMLA falls back to
a plain invite plus the older use-count tracking in invite_roles_cog, which assigns the role when someone joins.

Everything is idempotent: invites are reused while they still exist and still point at the same role; the links are
kept in ONE message per channel that is edited in place (no duplicate messages, no duplicate links).
"""

import asyncio
import logging

import discord
from discord.ext import commands

import invite_roles_cog
from config import config_manager, kamla_role
from naming import CH_LINKS
from ratelimit import bulk, describe

log = logging.getLogger("kamla.links")

HEADER = "🔗 **Role invitation links**"
FOOTER = ("These links never expire and have no use limit. Share each one only with the people who should get "
          "that role — the organiser links especially.")
KEY_KAMLA = "kamla"

# key, label, role name (None = KAMLA's own role), always shown
ENTRIES = [
    ("org", "ORG", "ORG"),
    ("captain", "CAP", "CAP"),
    ("tabby", "TABBY", "TABBY"),
    ("equity", "EQUITY", "EQUITY"),
    ("invited", "INVITED ADJUDICATOR", "INVITED ADJUDICATOR"),
    ("independent", "INDEPENDENT ADJUDICATOR", "INDEPENDENT ADJUDICATOR"),
    ("debater", "DEBATER", "DEBATER"),
    ("visitor", "VISITOR", "VISITOR"),
    (KEY_KAMLA, "KAMLA", None),
]
DEBOUNCE_SECONDS = 10.0

_refresh_tasks: dict[int, asyncio.Task] = {}
_locks: dict[int, asyncio.Lock] = {}


async def _http_create_invite(channel, payload: dict, reason: str) -> dict:
    """POST /channels/{id}/invites with a raw payload (role_ids is not in discord.py's create_invite)."""
    route = discord.http.Route("POST", "/channels/{channel_id}/invites", channel_id=channel.id)
    return await channel._state.http.request(route, json=payload, reason=reason)


async def create_invite(channel, role, label: str) -> dict:
    """A permanent, unlimited, unique invite for `role`.

    Returns {"code", "url", "native"}; native=True means Discord itself grants the role on acceptance."""
    reason = f"KAMLA role invite: {label}"
    if role is not None:
        payload = {"max_age": 0, "max_uses": 0, "unique": True, "role_ids": [str(role.id)]}
        try:
            data = await bulk.run(lambda: _http_create_invite(channel, payload, reason), label=f"role invite {label}")
            code = data["code"]
            return {"code": code, "url": f"https://discord.gg/{code}", "native": True}
        except discord.HTTPException as e:
            log.info("Discord refused a role-granting invite for %s (%s); using a plain invite.", label, describe(e))
    inv = await bulk.run(lambda: channel.create_invite(max_age=0, max_uses=0, unique=True, reason=reason),
                         label=f"create invite {label}")
    return {"code": inv.code, "url": inv.url, "native": False}


def _lock(guild_id: int) -> asyncio.Lock:
    return _locks.setdefault(guild_id, asyncio.Lock())


async def ensure_role_invites(guild: discord.Guild, channel) -> list[dict]:
    """Create or reuse the invite of every available role. Returns display rows:
    {key, label, role (or None), code, url, native, available}. Persists the mapping in the config."""
    cfg = await config_manager.get_config(guild)
    stored: dict = dict(cfg.get("invite_roles") or {})
    live = {i.code: i for i in await bulk.run(guild.invites, label="list invites")}
    by_key = {v.get("key"): (c, v) for c, v in stored.items() if v.get("key")}
    mapping = {c: v for c, v in stored.items() if v.get("key") == "dev"}          # legacy entry, kept as is

    rows = []
    for key, label, role_name in ENTRIES:
        role = kamla_role(guild) if role_name is None else next(
            (r for r in guild.roles if r.name == role_name and not r.managed), None)
        if role is None:
            rows.append({"key": key, "label": label, "role": None, "available": False})
            continue
        code, entry = by_key.get(key, (None, None))
        if code and code in live and entry.get("role_id") == role.id:
            native, url = bool(entry.get("native")), live[code].url
        else:
            if code and code in live:                          # the role was replaced: retire only OUR old invite
                await bulk.best_effort(lambda i=live[code]: i.delete(reason="KAMLA: role changed"), label="delete invite")
            made = await create_invite(channel, role, label)
            code, url, native = made["code"], made["url"], made["native"]
        mapping[code] = {"key": key, "roles": [role.name], "role_id": role.id, "native": native}
        rows.append({"key": key, "label": label, "role": role, "code": code, "url": url, "native": native,
                     "available": True})

    if mapping != stored:
        await config_manager.update_config(guild, invite_roles=mapping)
    try:                                                       # start/refresh use-count tracking for non-native links
        if any(not v.get("native") for v in mapping.values()):
            await invite_roles_cog._snapshot(guild)
            invite_roles_cog._tracked.add(guild.id)
    except Exception as e:  # noqa: BLE001
        log.debug("invite tracking not started: %s", describe(e))
    return rows


def render_links(rows: list[dict]) -> list[str]:
    """The message text(s): organised by role, one link each. Split at role boundaries to stay under 2000 characters."""
    blocks = []
    for r in rows:
        if not r["available"]:
            blocks.append(f"**{r['label']}** — role not available yet")
            continue
        note = ""
        if not r["native"]:
            if r["key"] == KEY_KAMLA:
                note = ("\n*Discord cannot give members a bot's own role, so this invite joins the server without "
                        "granting a role.*")
            else:
                note = "\n*Role is assigned by KAMLA when the member joins with this link.*"
        blocks.append(f"**{r['label']}**\n{r['url']}{note}")
    messages, current = [], HEADER + "\n\n"
    for block in blocks:
        if len(current) + len(block) + 2 > 1900:
            messages.append(current.rstrip())
            current = ""
        current += block + "\n\n"
    current += FOOTER
    messages.append(current.rstrip())
    return messages


async def _existing_messages(channel, me_id: int, ids: list[int]) -> list[discord.Message]:
    found = []
    for mid in ids:
        try:
            found.append(await channel.fetch_message(int(mid)))
        except (discord.NotFound, discord.HTTPException):
            pass
    if found:
        return found
    try:                                                    # config lost the ids: recognise our own earlier message
        async for m in channel.history(limit=50):
            if m.author.id == me_id and (m.content or "").startswith(HEADER):
                found.append(m)
    except discord.HTTPException:
        pass
    return list(reversed(found))


async def refresh_links(guild: discord.Guild, installer_id: int | None = None) -> list[dict]:
    """Create/update the links channel content. Safe to call any time; serialised per guild."""
    channel = discord.utils.get(guild.text_channels, name=CH_LINKS)
    if channel is None:
        raise RuntimeError("The links channel does not exist.")
    async with _lock(guild.id):
        rows = await ensure_role_invites(guild, channel)
        texts = render_links(rows)
        cfg = await config_manager.get_config(guild)
        ids = [int(i) for i in cfg.get("links_message_ids", [])]
        existing = await _existing_messages(channel, guild.me.id, ids)
        new_ids = []
        for i, text in enumerate(texts):
            if i < len(existing):
                msg = existing[i]
                if msg.content != text:
                    await bulk.run(lambda m=msg, t=text: m.edit(content=t, suppress=True), label="edit links")
            else:
                msg = await bulk.run(lambda t=text: channel.send(t, suppress_embeds=True,
                                                                 allowed_mentions=discord.AllowedMentions.none()),
                                     label="send links")
            new_ids.append(msg.id)
        for extra in existing[len(texts):]:                 # fewer messages than before: remove OUR surplus ones only
            await bulk.best_effort(extra.delete, label="delete surplus links message")
        if new_ids != ids:
            await config_manager.update_config(guild, links_message_ids=new_ids)
        return rows


async def ensure_kamla_invite(guild: discord.Guild) -> str:
    """The permanent 'KAMLA' invite (shown as a button on the developer log). '' when it cannot be made."""
    channel = discord.utils.get(guild.text_channels, name=CH_LINKS)
    if channel is None:
        return ""
    try:
        rows = await refresh_links(guild)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not prepare the KAMLA invite for guild %s: %s", guild.id, describe(e))
        return ""
    return next((r["url"] for r in rows if r["key"] == KEY_KAMLA and r["available"]), "")


class LinksCog(commands.Cog):
    """Keeps the links channel in step with the server's roles and invites (debounced, one refresh per burst)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_unload(self) -> None:
        for t in list(_refresh_tasks.values()):
            t.cancel()
        _refresh_tasks.clear()

    def _schedule(self, guild: discord.Guild) -> None:
        if discord.utils.get(guild.text_channels, name=CH_LINKS) is None:
            return
        task = _refresh_tasks.get(guild.id)
        if task is not None and not task.done():
            return

        async def later():
            try:
                await asyncio.sleep(DEBOUNCE_SECONDS)
                await refresh_links(guild)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Could not update the links channel of guild %s: %s", guild.id, describe(e))
            finally:
                _refresh_tasks.pop(guild.id, None)
        _refresh_tasks[guild.id] = asyncio.get_running_loop().create_task(later())

    @staticmethod
    def _relevant_role(role: discord.Role) -> bool:
        from config import KAMLA_ROLE_NAMES
        return role.name in KAMLA_ROLE_NAMES or getattr(role, "managed", False)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        if self._relevant_role(role):
            self._schedule(role.guild)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if self._relevant_role(role):
            self._schedule(role.guild)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if before.name != after.name and (self._relevant_role(before) or self._relevant_role(after)):
            self._schedule(after.guild)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        """One of our links was revoked by someone: put a fresh one back."""
        guild = invite.guild
        if guild is None or not hasattr(guild, "text_channels"):
            return
        cfg = config_manager.get_cached(guild.id) or {}
        if invite.code in (cfg.get("invite_roles") or {}):
            self._schedule(guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(LinksCog(bot))

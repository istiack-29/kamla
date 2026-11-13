"""
invite_roles_cog.py — role-granting invite links.

After a server build KAMLA creates five permanent, unlimited-use invites in the welcome
channel and DMs them to whoever ran the build. Each link grants exactly ONE role (a member
can only ever hold one KAMLA role; assignment goes through assign_kamla_role, which removes
any other KAMLA role first):

    [V] VISITOR   [CA] CAP   [T] TABBY   [O] ORG   [E] EQUITY     (CAP/TABBY/ORG/EQUITY get organiser facilities, never Administrator)

Discord invites cannot carry roles, so KAMLA works out which invite a new member used by
comparing invite use-counts before/after the join, then assigns that invite's role(s).
Safety rules:
  * A join is only matched when the result is unambiguous. If several different invites
    changed at once and several members joined, NO role is assigned (and a warning logged)
    rather than guessing — a wrong guess on the staff link would hand out admin roles.
  * Joins that arrive together are batched, so a rush of members costs one invite lookup.
  * Guilds without role-invites cost zero requests.
The invite-code -> roles mapping lives in the existing kamla-config channel.
"""

import asyncio
from typing import Optional
import logging

import discord
from discord.ext import commands

from config import config_manager, assign_kamla_role
from ratelimit import bulk, notify, describe

log = logging.getLogger("kamla.invites")

WELCOME_CHANNEL = "👋🏻︱welcome"
BATCH_DELAY = 1.0

# key -> (label shown to the user, roles granted)
GROUPS = [
    ("visitor", "[V] Visitor", ["VISITOR"]),
    ("captain", "[CA] Captain", ["CAP"]),
    ("tabby",   "[T] Tabby",   ["TABBY"]),
    ("org",     "[O] Org",     ["ORG"]),
    ("equity",  "[E] Equity",  ["EQUITY"]),
]
ADMIN_LABELS = "[CA], [T], [O] and [E]"      # organiser roles (no Administrator permission)

DEV_KEY = "dev"
DEV_ROLE_NAME = "KAMLA"

# guild_id -> {invite code: uses}. In memory only; rebuilt from Discord on startup.
_uses: dict[int, dict[str, int]] = {}
_tracked: set[int] = set()

# (guild, member) -> which role-invite KEY ("visitor", ..., "dev") the member joined with, or None when
# unknown. Other listeners (the welcome message) wait for it so they can treat developer joins differently.
_outcomes: dict[tuple[int, int], "asyncio.Future"] = {}
OUTCOME_TIMEOUT = 6.0


def _outcome_future(guild_id: int, member_id: int) -> "asyncio.Future":
    key = (guild_id, member_id)
    fut = _outcomes.get(key)
    if fut is None or fut.done() and fut.cancelled():
        loop = asyncio.get_running_loop()
        fut = _outcomes[key] = loop.create_future()
        loop.call_later(120, lambda: _outcomes.pop(key, None))        # never leak entries nobody asked for
    return fut


def _set_outcome(guild_id: int, member_id: int, key: Optional[str]) -> None:
    fut = _outcome_future(guild_id, member_id)
    if not fut.done():
        fut.set_result(key)


async def join_outcome(member: discord.Member, timeout: Optional[float] = None) -> Optional[str]:
    """The role-invite key a new member joined with ('dev' = the developer invite), or None if it is not a
    role invite / could not be determined. Returns immediately for servers without role invites."""
    if member.bot or member.guild.id not in _tracked:
        return None
    fut = _outcome_future(member.guild.id, member.id)
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout if timeout is not None else OUTCOME_TIMEOUT)
    except asyncio.TimeoutError:
        return None
    finally:
        _outcomes.pop((member.guild.id, member.id), None)


async def _snapshot(guild: discord.Guild) -> dict[str, int]:
    invites = await bulk.run(guild.invites, label="list invites")
    snap = {i.code: (i.uses or 0) for i in invites}
    _uses[guild.id] = dict(snap)
    return snap


async def create_role_invites(guild: discord.Guild) -> list[dict]:
    """Create (or reuse still-valid) the five invites. Returns
    [{key, label, roles, code, url}], in display order. Idempotent: re-running after an
    interrupted build reuses invites that still exist instead of making duplicates."""
    channel = discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL)
    if channel is None:
        raise RuntimeError("The welcome channel does not exist, so the invite links could not be created.")

    cfg = await config_manager.get_config(guild)
    stored: dict = dict(cfg.get("invite_roles") or {})
    live = {i.code: i for i in await bulk.run(guild.invites, label="list invites")}

    result = []
    mapping = {c: v for c, v in stored.items() if v.get("key") == DEV_KEY and c in live}   # keep the developer's invite
    for key, label, roles in GROUPS:
        code = next((c for c, v in stored.items()
                     if v.get("key") == key and c in live), None)
        if code is None:
            inv = await bulk.run(
                lambda: channel.create_invite(max_age=0, max_uses=0, unique=True,
                                              reason=f"KAMLA role invite: {label}"),
                label=f"create invite {key}")
            code, url = inv.code, inv.url
        else:
            url = live[code].url
        mapping[code] = {"key": key, "roles": roles}
        result.append({"key": key, "label": label, "roles": roles, "code": code, "url": url})

    await config_manager.update_config(guild, invite_roles=mapping)
    await _snapshot(guild)
    _tracked.add(guild.id)
    return result


async def ensure_dev_invite(guild: discord.Guild, owner_id: int) -> str:
    """The developer's way into a server where KAMLA runs. Returns an invite URL ('' on failure).

    * makes sure the visible "KAMLA" administrator role exists (and gives it to the developer if they are
      already a member — e.g. after a rebuild wiped the role);
    * keeps one permanent invite whose join is mapped to that role. The role is assigned ONLY to the
      developer's own account (checked on join), so anybody else who uses the link just joins normally.
    """
    role = next((r for r in guild.roles if r.name == DEV_ROLE_NAME and not r.managed), None)
    if role is None:
        role = await bulk.run(
            lambda: guild.create_role(name=DEV_ROLE_NAME, permissions=discord.Permissions(administrator=True),
                                      color=discord.Color.dark_gray(), reason="KAMLA developer role"),
            label="create KAMLA role")
        top = guild.me.top_role.position
        await bulk.best_effort(lambda: role.edit(position=max(1, top - 1), reason="KAMLA developer role"),
                               label="move KAMLA role")
    member = guild.get_member(owner_id)
    if member is not None and role not in member.roles:
        await bulk.best_effort(lambda: member.add_roles(role, reason="KAMLA developer role"), label="give KAMLA role")

    channel = discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL)
    if channel is None:
        channel = next((c for c in guild.text_channels
                        if c.permissions_for(guild.me).create_instant_invite), None)
    if channel is None:
        return ""

    cfg = await config_manager.get_config(guild)
    stored: dict = dict(cfg.get("invite_roles") or {})
    live = {i.code: i for i in await bulk.run(guild.invites, label="list invites")}
    code = next((c for c, v in stored.items() if v.get("key") == DEV_KEY and c in live
                 and v.get("only_user") == owner_id), None)
    if code is None:
        inv = await bulk.run(
            lambda: channel.create_invite(max_age=0, max_uses=0, unique=True, reason="KAMLA developer invite"),
            label="create dev invite")
        code, url = inv.code, inv.url
    else:
        url = live[code].url
    updated = {c: v for c, v in stored.items() if v.get("key") != DEV_KEY or c == code}
    updated[code] = {"key": DEV_KEY, "roles": [DEV_ROLE_NAME], "only_user": owner_id}
    if updated != stored:                         # write the config only when something changed
        await config_manager.update_config(guild, invite_roles=updated)
        await _snapshot(guild)
    _tracked.add(guild.id)
    return url


def dm_text(tournament: str, links: list[dict]) -> str:
    lines = [f"🔗 **Invite links for {tournament}**", ""]
    for item in links:
        lines.append(f"**{item['label']}**")
        lines.append(f"<{item['url']}>")
        lines.append("")
    lines.append("These links never expire and can be used any number of times. "
                 "Anyone who joins with a link gets the role shown above (one role per person).")
    lines.append("No link grants administrator permission, and members cannot create invites themselves.")
    lines.append(f"The {ADMIN_LABELS} links give organiser facilities inside the tournament — "
                 "send them only to the right people.")
    lines.append("")
    lines.append("Please send the links as per your need.")
    return "\n".join(lines)


class InviteRolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending: dict[int, list[discord.Member]] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._primed = False
        self._prime_task: asyncio.Task | None = None

    async def cog_unload(self) -> None:
        for t in list(self._workers.values()) + ([self._prime_task] if self._prime_task else []):
            if t and not t.done():
                t.cancel()
        self._workers.clear()

    # ── startup: learn which guilds use role invites (once, paced) ──────────

    @commands.Cog.listener()
    async def on_ready(self):
        if self._primed:                     # on_ready repeats after reconnects
            return
        self._primed = True
        self._prime_task = asyncio.get_running_loop().create_task(self._prime_all())

    async def _prime_all(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                cfg = await config_manager.get_config(guild)
                if cfg.get("invite_roles"):
                    await _snapshot(guild)
                    _tracked.add(guild.id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Could not prepare invite tracking for guild %s: %s", guild.id, describe(e))

    # ── keep use-counts current without REST calls ──────────────────────────

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild and invite.guild.id in _tracked:
            _uses.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild:
            _uses.get(invite.guild.id, {}).pop(invite.code, None)

    # ── a member joined: which invite did they use? ─────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or member.guild.id not in _tracked:
            return
        self._pending.setdefault(member.guild.id, []).append(member)
        worker = self._workers.get(member.guild.id)
        if worker is None or worker.done():
            self._workers[member.guild.id] = asyncio.get_running_loop().create_task(
                self._process(member.guild))

    async def _process(self, guild: discord.Guild) -> None:
        try:
            while self._pending.get(guild.id):
                await asyncio.sleep(BATCH_DELAY)          # let simultaneous joins batch up
                members = self._pending.pop(guild.id, [])
                if members:
                    await self._assign_batch(guild, members)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Invite-role processing failed in guild %s: %s", guild.id, describe(e))

    async def _assign_batch(self, guild: discord.Guild, members: list[discord.Member]) -> None:
        key: Optional[str] = None
        try:
            key = await self._assign_batch_inner(guild, members)
        finally:
            for m in members:                      # always release anyone waiting for the outcome
                _set_outcome(guild.id, m.id, key)

    async def _assign_batch_inner(self, guild: discord.Guild, members: list[discord.Member]) -> Optional[str]:
        before = dict(_uses.get(guild.id, {}))
        now = await _snapshot(guild)
        deltas = {c: n - before.get(c, 0) for c, n in now.items() if n > before.get(c, 0)}
        cfg = await config_manager.get_config(guild)
        mapping: dict = cfg.get("invite_roles") or {}

        if not deltas:
            log.info("No tracked invite use detected for %d new member(s) in guild %s.", len(members), guild.id)
            return None
        total = sum(deltas.values())
        # Strict rule: exactly ONE invite changed and its use count equals the joins we saw.
        # Anything else (several invites, joins we didn't witness, vanity URL joins mixed in) is
        # ambiguous, and guessing wrong on an organiser link would hand out organiser facilities.
        if len(deltas) != 1 or total != len(members):
            log.warning("Ambiguous join in guild %s (%d member(s) seen, %d invite use(s) across %d invite(s)). "
                        "No invite roles were assigned.", guild.id, len(members), total, len(deltas))
            return None

        code = next(iter(deltas))
        entry = mapping.get(code)
        if not entry:
            return None                                   # a normal, non-role invite
        role = next((r for r in guild.roles if r.name == entry["roles"][0] and not r.managed), None)
        if role is None:
            log.warning("Role %s for invite %s is missing in guild %s.", entry["roles"][0], code, guild.id)
            return entry["key"]
        for member in members:
            only = entry.get("only_user")
            if only and member.id != only:
                log.warning("Someone other than the developer used the developer invite in guild %s; "
                            "no role was given.", guild.id)
                continue
            try:
                if entry["key"] == DEV_KEY:
                    # the developer role is added on top of whatever else they hold (no one-role rule)
                    await notify.run(lambda m=member: m.add_roles(role, reason="KAMLA developer invite"),
                                     label="dev-invite-role")
                else:
                    # one KAMLA role per person: removes any other KAMLA role, then adds this one
                    await assign_kamla_role(member, role)
                log.info("Assigned %s to a new member via the %s invite.", role.name, entry["key"])
            except Exception as e:  # noqa: BLE001
                log.warning("Could not assign invite role: %s", describe(e))
        return entry["key"]


async def setup(bot: commands.Bot):
    await bot.add_cog(InviteRolesCog(bot))

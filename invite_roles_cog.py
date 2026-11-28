"""
invite_roles_cog.py — fallback tracking for role-invite links.

The links themselves are created and posted by links_cog.py (🔗︱links), preferably with Discord's native
role-granting invites (the `role_ids` invite parameter): Discord then gives the role on acceptance and nothing here
needs to run. This module is only the FALLBACK for a link that Discord would not create with a role: it works out
which invite a new member used by comparing invite use-counts before/after the join and assigns that link's tournament
role (one KAMLA tournament role per person, through assign_kamla_role).

Safety rules: a join is only matched when the result is unambiguous (several invites changing at once with several
joins assigns NOTHING rather than guessing — a wrong guess on an organiser link would hand out organiser roles),
joins arriving together are batched into one lookup, and guilds without fallback links cost zero requests.
KAMLA has exactly one role of its own — the bot's integration role; no second "KAMLA" role is ever created.
"""

import asyncio
from typing import Optional
import logging

import discord
from discord.ext import commands

from config import config_manager, assign_kamla_role
from ratelimit import bulk, describe

log = logging.getLogger("kamla.invites")

BATCH_DELAY = 1.0
DEV_KEY = "dev"          # legacy mapping entry of older versions; never assigned any more

# guild_id -> {invite code: uses}. In memory only; rebuilt from Discord on startup.
_uses: dict[int, dict[str, int]] = {}
_tracked: set[int] = set()

async def _snapshot(guild: discord.Guild) -> dict[str, int]:
    invites = await bulk.run(guild.invites, label="list invites")
    snap = {i.code: (i.uses or 0) for i in invites}
    _uses[guild.id] = dict(snap)
    return snap


async def ensure_dev_invite(guild: discord.Guild, owner_id: int | None = None) -> str:
    """The permanent KAMLA invite used by the developer log's button ('' on failure). No role is created or
    given: KAMLA's only role is its own integration role, which Discord does not allow to be assigned."""
    from links_cog import ensure_kamla_invite
    return await ensure_kamla_invite(guild)


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
        await self._assign_batch_inner(guild, members)

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
        if entry.get("native") or entry.get("key") in ("kamla", DEV_KEY):
            return entry.get("key")                       # Discord grants native roles itself; KAMLA's role is not assignable
        role = next((r for r in guild.roles if r.name == entry["roles"][0] and not r.managed), None)
        if role is None:
            log.warning("Role %s for invite %s is missing in guild %s.", entry["roles"][0], code, guild.id)
            return entry["key"]
        for member in members:
            try:
                # one tournament role per person: removes any other KAMLA role, then adds this one
                if await assign_kamla_role(member, role):
                    log.info("Assigned %s to a new member via the %s invite.", role.name, entry["key"])
            except Exception as e:  # noqa: BLE001
                log.warning("Could not assign invite role: %s", describe(e))
        return entry["key"]


async def setup(bot: commands.Bot):
    await bot.add_cog(InviteRolesCog(bot))

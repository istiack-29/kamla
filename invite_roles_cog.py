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
import logging

import discord
from discord.ext import commands

from config import config_manager, assign_kamla_role
from ratelimit import bulk, describe

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

# guild_id -> {invite code: uses}. In memory only; rebuilt from Discord on startup.
_uses: dict[int, dict[str, int]] = {}
_tracked: set[int] = set()


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

    result, mapping = [], {}
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
        before = dict(_uses.get(guild.id, {}))
        now = await _snapshot(guild)
        deltas = {c: n - before.get(c, 0) for c, n in now.items() if n > before.get(c, 0)}
        cfg = await config_manager.get_config(guild)
        mapping: dict = cfg.get("invite_roles") or {}

        if not deltas:
            log.info("No tracked invite use detected for %d new member(s) in guild %s.", len(members), guild.id)
            return
        total = sum(deltas.values())
        # Strict rule: exactly ONE invite changed and its use count equals the joins we saw.
        # Anything else (several invites, joins we didn't witness, vanity URL joins mixed in) is
        # ambiguous, and guessing wrong on an organiser link would hand out organiser facilities.
        if len(deltas) != 1 or total != len(members):
            log.warning("Ambiguous join in guild %s (%d member(s) seen, %d invite use(s) across %d invite(s)). "
                        "No invite roles were assigned.", guild.id, len(members), total, len(deltas))
            return

        code = next(iter(deltas))
        entry = mapping.get(code)
        if not entry:
            return                                        # a normal, non-role invite
        role = discord.utils.get(guild.roles, name=entry["roles"][0])
        if role is None:
            log.warning("Role %s for invite %s is missing in guild %s.", entry["roles"][0], code, guild.id)
            return
        for member in members:
            try:
                # one KAMLA role per person: removes any other KAMLA role, then adds this one
                await assign_kamla_role(member, role)
                log.info("Assigned %s to a new member via the %s invite.", role.name, entry["key"])
            except Exception as e:  # noqa: BLE001
                log.warning("Could not assign invite role: %s", describe(e))


async def setup(bot: commands.Bot):
    await bot.add_cog(InviteRolesCog(bot))

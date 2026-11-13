"""
nickname_cog.py — keeps a role tag at the start of every member's nickname.

    [O] ORG   [CA] CAP   [T] TABBY   [E] EQUITY
    [J] INVITED / INDEPENDENT ADJUDICATOR   [D] DEBATER   [V] VISITOR

* Automatic: whenever a member's roles (or nickname) change, the tag is re-applied.
  Changes are debounced per member, so the role swap KAMLA performs (remove old role,
  add new one) results in ONE nickname edit, not two.
* /nicknames: admins can sync everyone who is already in the server (paced).
* Members who lost every KAMLA role get their tag removed again.
* Zero-request fast path: members whose nickname is already correct are skipped
  locally, and the server owner / members above the bot are skipped *before* calling
  Discord (a guaranteed 403 would only count towards Discord's invalid-request limit).
"""

import asyncio
import logging
import os
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import ADMIN_ROLE_NAMES
from ratelimit import bulk, describe

log = logging.getLogger("kamla.nick")

# Highest priority first: a member who somehow holds several roles gets the most senior tag.
PRIORITY = [
    ("ORG", "[O]"),
    ("CAP", "[CA]"),
    ("TABBY", "[T]"),
    ("EQUITY", "[E]"),
    ("INDEPENDENT ADJUDICATOR", "[J]"),
    ("INVITED ADJUDICATOR", "[J]"),
    ("DEBATER", "[D]"),
    ("VISITOR", "[V]"),
]
_PREFIX_RE = re.compile(r"^\s*\[(?:O|CA|T|E|J|D|V)\]\s*")
MAX_NICK = 32
DEBOUNCE_SECONDS = 2.0
STARTUP_DELAY = 30.0          # let the bot settle before the start-up reconcile
STARTUP_GAP = 2.0             # pause between servers


def prefix_for(member) -> str | None:
    names = {r.name for r in member.roles}
    for role_name, prefix in PRIORITY:
        if role_name in names:
            return prefix
    return None


def strip_prefix(name: str) -> str:
    previous = None
    while previous != name:
        previous = name
        name = _PREFIX_RE.sub("", name, count=1)
    return name.strip()


def _natural_name(member) -> str:
    return getattr(member, "global_name", None) or member.name


def desired_nick(member) -> str | None:
    """The nickname the member SHOULD have (None = no nickname)."""
    base = strip_prefix(member.nick or _natural_name(member)) or member.name
    prefix = prefix_for(member)
    if prefix:
        room = MAX_NICK - len(prefix) - 1
        return f"{prefix} {base[:room].rstrip()}"
    if member.nick and _PREFIX_RE.match(member.nick):      # lost their role: remove our tag
        return None if base == _natural_name(member) else base[:MAX_NICK]
    return member.nick                                       # nothing of ours to touch


REASON_PERMISSION = "KAMLA lacks the Manage Nicknames permission"
REASON_OWNER = "server owner (Discord never lets a bot change the owner's nickname)"
REASON_HIERARCHY = "their top role is above KAMLA's role"


def skip_reason(member) -> str | None:
    """Why KAMLA cannot change this member's nickname, or None if it can. Pure/local (no API request).
    Sending a request that is certain to fail would only count against Discord's invalid-request limit."""
    guild = member.guild
    perms = getattr(guild.me, "guild_permissions", None)
    if perms is not None and not (perms.administrator or perms.manage_nicknames):
        return REASON_PERMISSION
    if member.id == guild.owner_id:
        return REASON_OWNER
    if member.top_role.position >= guild.me.top_role.position:
        return REASON_HIERARCHY
    return None


def plan(member) -> tuple[str, str | None]:
    """('ok'|'skip'|'edit', nick). Pure/local: costs no API request."""
    if member.bot:
        return "skip", None
    desired = desired_nick(member)
    if desired == member.nick:
        return "ok", None
    if skip_reason(member):
        return "skip", None
    return "edit", desired


async def sync_member(member) -> str:
    """Returns 'updated' | 'ok' | 'skipped' | 'failed'. Every non-obvious outcome is logged with its reason,
    so "the nickname did not change" can always be explained from the logs."""
    action, nick = plan(member)
    if action == "ok":
        return "ok"
    if action == "skip":
        log.info("Nickname of %s left unchanged: %s.", member.id, skip_reason(member) or "bot account")
        return "skipped"
    try:
        await bulk.run(lambda: member.edit(nick=nick, reason="KAMLA role tag"), label="nickname")
        log.info("Nickname of %s set to %r.", member.id, nick)
        return "updated"
    except discord.Forbidden:
        log.warning("Discord refused the nickname change for %s (403). In Server Settings → Roles, move KAMLA's "
                    "role above every tournament role and make sure it has Manage Nicknames.", member.id)
        return "skipped"
    except Exception as e:  # noqa: BLE001
        log.warning("Could not set nickname for %s: %s", member.id, describe(e))
        return "failed"


class NicknameCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._rerun: set[tuple[int, int]] = set()
        self._sweeping: set[int] = set()
        self._startup_task: asyncio.Task | None = None
        self._startup_done = False

    async def cog_unload(self) -> None:
        for t in list(self._tasks.values()) + ([self._startup_task] if self._startup_task else []):
            if t and not t.done():
                t.cancel()
        self._tasks.clear()

    # ── start-up reconcile ──────────────────────────────────────────────────
    # Role changes that happened while KAMLA was offline (or before this feature existed) never produced an
    # event, so those members would stay untagged until someone ran /nicknames. After every start KAMLA
    # therefore makes one quiet, paced pass: only members who actually need a change cost a request.
    # Disable with KAMLA_NICKNAME_STARTUP_SYNC=0.

    @commands.Cog.listener()
    async def on_ready(self):
        if self._startup_done or os.getenv("KAMLA_NICKNAME_STARTUP_SYNC", "1").strip() == "0":
            return
        self._startup_done = True                    # on_ready repeats after reconnects; do this once
        self._startup_task = asyncio.get_running_loop().create_task(self._startup_sweep())

    async def _startup_sweep(self) -> None:
        try:
            await asyncio.sleep(STARTUP_DELAY)
            for guild in list(self.bot.guilds):
                todo = [m for m in guild.members if plan(m)[0] == "edit"]
                changed = 0
                for member in todo:
                    if await sync_member(member) == "updated":
                        changed += 1
                if todo:
                    log.info("Start-up nickname reconcile in guild %s: %d of %d needed update.", guild.id, changed, len(todo))
                await asyncio.sleep(STARTUP_GAP)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Start-up nickname reconcile failed: %s", describe(e))

    # ── automatic: on role / nickname change ────────────────────────────────

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if after.bot:
            return
        if before.roles == after.roles and before.nick == after.nick:
            return
        self._schedule(after)

    def _schedule(self, member: discord.Member) -> None:
        key = (member.guild.id, member.id)
        running = self._tasks.get(key)
        if running and not running.done():
            self._rerun.add(key)           # state changed again meanwhile: re-check once more
            return

        async def job() -> None:
            try:
                while True:
                    self._rerun.discard(key)
                    await asyncio.sleep(DEBOUNCE_SECONDS)
                    current = member.guild.get_member(member.id)
                    if current is not None:
                        await sync_member(current)
                    if key not in self._rerun:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Nickname sync failed for %s: %s", member.id, describe(e))
            finally:
                self._tasks.pop(key, None)

        self._tasks[key] = asyncio.get_running_loop().create_task(job())

    # ── /nicknames: sync everyone already in the server ─────────────────────

    @app_commands.command(name="nicknames", description="Set the [O]/[CA]/[T]/[E]/[J]/[D]/[V] role tag in everyone's nickname")
    @app_commands.guild_only()
    async def nicknames(self, interaction: discord.Interaction):
        user = interaction.user
        if not (user.guild_permissions.administrator or any(r.name in ADMIN_ROLE_NAMES for r in user.roles)):
            await interaction.response.send_message("⛔ Only admins can use this command.", ephemeral=True)
            return
        guild = interaction.guild
        if guild.id in self._sweeping:
            await interaction.response.send_message("⏳ A nickname sync is already running.", ephemeral=True)
            return
        self._sweeping.add(guild.id)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if not getattr(guild, "chunked", True):
                try:
                    await guild.chunk()               # make sure every member is known, not just the cached ones
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not load the full member list of guild %s: %s", guild.id, describe(e))
            members = [m for m in guild.members if not m.bot]
            plans = [(m, plan(m)[0]) for m in members]
            todo = [m for m, action in plans if action == "edit"]
            counts = {
                "updated": 0, "failed": 0,
                "ok": sum(1 for _, a in plans if a == "ok"),
                # owner / above the bot: skipped locally, no doomed request is sent
                "skipped": sum(1 for _, a in plans if a == "skip"),
            }

            last = time.monotonic()
            for i, member in enumerate(todo, 1):
                result = await sync_member(member)
                counts[result] += 1
                if time.monotonic() - last > 5:
                    last = time.monotonic()
                    try:
                        await interaction.edit_original_response(
                            content=f"⏳ Updating nicknames… {i}/{len(todo)}")
                    except Exception:  # noqa: BLE001 - progress is cosmetic
                        pass

            note = ""
            skipped = [(m, skip_reason(m)) for m, a in plans if a == "skip" and skip_reason(m)]
            if skipped:
                reasons: dict[str, list] = {}
                for m, why in skipped:
                    reasons.setdefault(why, []).append(m)
                lines = []
                for why, ms in reasons.items():
                    who = ", ".join(getattr(m, "display_name", str(m.id)) for m in ms[:6]) + (" …" if len(ms) > 6 else "")
                    lines.append(f"• **{len(ms)}** — {why}: {who}")
                note = "\n\nNot changed:\n" + "\n".join(lines)
                if REASON_HIERARCHY in reasons or REASON_PERMISSION in reasons:
                    note += ("\n\n**Fix:** Server Settings → Roles → drag **KAMLA's role above all tournament roles** "
                             "(and keep Manage Nicknames or Administrator on it).")
            elif not members or not any(a == "edit" or a == "ok" for _, a in plans):
                note = "\n\nNo members with a KAMLA role were found."
            await interaction.edit_original_response(content=(
                f"✅ Nicknames synced: **{counts['updated']}** updated, **{counts['ok']}** already correct, "
                f"**{counts['skipped']}** skipped, **{counts['failed']}** failed.{note}")[:1900])
        except Exception as e:  # noqa: BLE001
            log.exception("Nickname sync failed in guild %s", guild.id)
            try:
                await interaction.edit_original_response(content=f"❌ Nickname sync failed: {describe(e)}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._sweeping.discard(guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(NicknameCog(bot))

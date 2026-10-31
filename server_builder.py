import asyncio
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import discord
from config import (
    KAMLA_ROLE_NAMES, ADMIN_ROLE_NAMES, config_manager
)
from ratelimit import bulk, classify, describe, Kind

log = logging.getLogger("kamla.builder")

_building_guilds: set[int] = set()
_restoring_guilds: set[int] = set()

ROLE_CONFIG = [
    {"name": "ORG",                    "color": discord.Color.red(),        "administrator": True,  "hoist": True},
    {"name": "CAP",                    "color": discord.Color.orange(),     "administrator": True,  "hoist": True},
    {"name": "TABBY",                  "color": discord.Color.yellow(),     "administrator": True,  "hoist": True},
    {"name": "EQUITY",                 "color": discord.Color.green(),      "administrator": True,  "hoist": True},
    {"name": "INVITED ADJUDICATOR",    "color": discord.Color.blue(),       "administrator": False, "hoist": True},
    {"name": "INDEPENDENT ADJUDICATOR","color": discord.Color.purple(),     "administrator": False, "hoist": True},
    {"name": "DEBATER",                "color": discord.Color.teal(),       "administrator": False, "hoist": True},
    {"name": "VISITOR",                "color": discord.Color.light_grey(), "administrator": False, "hoist": False},
]

ORG_ACCESS_ROLES = ["ORG", "CAP", "TABBY", "EQUITY", "INVITED ADJUDICATOR"]

# ── Protected structure ────────────────────────────────────────────────────────

PROTECTED_CATEGORIES = {
    "⚜️︱ORGCOM",
    "🛅︱ASSIGN",
    "🏟️︱GRAND AUDITORIUM",
    "🎮︱PLAY",
    "ℹ️︱INFORMATION",
}

PROTECTED_TOP_LEVEL_CHANNELS = {
    "👐🏻︱meet-the-developer",
    "👋🏻︱welcome",
    "❓︱get-role",
    "🦧︱how-to-use-this-server",
}

CATEGORY_CHANNEL_STRUCTURE: dict[str, dict] = {
    "⚜️︱ORGCOM": {
        "text":  ["⚙️︱settings", "🏴︱org", "📂︱document"],
        "voice": ["ORG", "Control Room"],
    },
    "🛅︱ASSIGN": {
        "text":  ["org", "cap", "tabby", "equity", "debater", "visitor",
                  "independent-adjudicator", "invited-adjudicator"],
        "voice": [],
    },
    "🏟️︱GRAND AUDITORIUM": {
        "text":  ["📨︱ga-text", "🎓︱motion", "🎓︱announcement", "🎓︱break",
                  "🎓︱matchup", "🎓︱ballot", "📊︱poll", "📊︱yes-no-voting"],
        "voice": ["🏟️︱GRAND AUDITORIUM", "😤︱CLASH-EQUITY ROOM"],
    },
    "🎮︱PLAY": {
        "text":  ["❌︱tic-tac-toe︱⭕", "🤛︱rock✊-paper📰-scissors✌️", "🪙︱toss-coin"],
        "voice": [],
    },
    "ℹ️︱INFORMATION": {
        "text":  ["schedule", "important-forms", "debater-briefing",
                  "judge-briefing", "equity-briefing"],
        "voice": [],
    },
}


def _allow(*perms) -> discord.PermissionOverwrite:
    ow = discord.PermissionOverwrite()
    for p in perms:
        setattr(ow, p, True)
    return ow


def _role_ow(admin: bool = False, view: bool = True, send: bool = True) -> discord.PermissionOverwrite:
    ow = discord.PermissionOverwrite(view_channel=view)
    if view:
        ow.read_message_history = True
    if send:
        ow.send_messages = True
        ow.attach_files = True
        ow.embed_links = True
    if admin:
        ow.manage_messages = True
    return ow


# ── Discord hard limits ────────────────────────────────────────────────────────
# Documented by Discord support / community references: 500 channels per server
# (categories included) and 50 categories per server. A request that exceeds them
# can never succeed, but the old code only discovered that after hundreds of calls
# and then a retry would wipe + rebuild from zero — a self-inflicted request storm.
DISCORD_MAX_CHANNELS = 500
DISCORD_MAX_CATEGORIES = 50

TOP_LEVEL_CHANNELS = [
    "👐🏻︱meet-the-developer",
    "👋🏻︱welcome",
    "❓︱get-role",
    "🦧︱how-to-use-this-server",
]
ASSIGN_ROLE_ORDER = ["ORG", "CAP", "TABBY", "EQUITY", "DEBATER", "VISITOR",
                     "INDEPENDENT ADJUDICATOR", "INVITED ADJUDICATOR"]
INFO_CHANNELS = ["schedule", "important-forms", "debater-briefing",
                 "judge-briefing", "equity-briefing"]
GA_ANNOUNCE_CHANNELS = ["🎓︱motion", "🎓︱announcement", "🎓︱break",
                        "🎓︱matchup", "🎓︱ballot", "📊︱poll", "📊︱yes-no-voting"]


def base_channel_count() -> int:
    """Channels (categories included) created by a fresh build, excluding ROOM categories."""
    total = len(TOP_LEVEL_CHANNELS)
    for structure in CATEGORY_CHANNEL_STRUCTURE.values():
        total += 1 + len(structure["text"]) + len(structure["voice"])
    return total


def room_channel_count(fmt: str) -> int:
    """Channels in one ROOM XX category, category itself included."""
    preps = 2 if fmt.lower() == "ap" else 4
    return 1 + 3 + 1 + preps + 1          # category + timer/poi/all-in + DEBATE ROOM + preps + ADJUDICATION


def max_rooms_for_fresh_build(fmt: str) -> int:
    """Largest room count a freshly wiped server can hold (the kamla-config channel survives a wipe)."""
    by_channels = (DISCORD_MAX_CHANNELS - 1 - base_channel_count()) // room_channel_count(fmt)
    by_categories = DISCORD_MAX_CATEGORIES - len(CATEGORY_CHANNEL_STRUCTURE)
    return max(0, min(by_channels, by_categories))


def capacity_problem_fresh_build(fmt: str, rooms: int) -> str | None:
    limit = max_rooms_for_fresh_build(fmt)
    if rooms <= limit:
        return None
    return (
        f"Discord allows at most {DISCORD_MAX_CHANNELS} channels and {DISCORD_MAX_CATEGORIES} categories per server, "
        f"so a {fmt.upper()} tournament can have at most **{limit}** rooms (you asked for {rooms}). "
        "Nothing was changed."
    )


def capacity_problem_add_room(guild: discord.Guild, fmt: str) -> str | None:
    if len(guild.channels) + room_channel_count(fmt) > DISCORD_MAX_CHANNELS:
        return f"This server would exceed Discord's {DISCORD_MAX_CHANNELS}-channel limit."
    if len(guild.categories) + 1 > DISCORD_MAX_CATEGORIES:
        return f"This server would exceed Discord's {DISCORD_MAX_CATEGORIES}-category limit."
    return None


# ── Job coordination ──────────────────────────────────────────────────────────

class BuildInProgress(RuntimeError):
    """Another structure-changing job is already running for this guild."""


_job_locks: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def building_job(guild: discord.Guild):
    """Exclusive per-guild structure job (build / add room / remove room / switch format).
    Two admins pressing buttons at once used to run two builds concurrently (duplicates,
    doubled traffic); the second now gets a clear message instead."""
    lock = _job_locks.setdefault(guild.id, asyncio.Lock())
    if lock.locked():
        raise BuildInProgress("A server build/change is already running. Wait for it to finish.")
    async with lock:
        added = guild.id not in _building_guilds
        _building_guilds.add(guild.id)
        try:
            yield
        finally:
            if added:
                _building_guilds.discard(guild.id)


@asynccontextmanager
async def _marked_building(guild: discord.Guild):
    """Keep the channel guard quiet while a wipe/build runs, even when called outside building_job."""
    added = guild.id not in _building_guilds
    _building_guilds.add(guild.id)
    try:
        yield
    finally:
        if added:
            _building_guilds.discard(guild.id)


# ── Reporting ─────────────────────────────────────────────────────────────────

@dataclass
class BuildReport:
    created: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    reconciled: list[str] = field(default_factory=list)
    failed: list[tuple[str, str, str]] = field(default_factory=list)   # (what, kind, detail)
    _perm_streak: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed

    def merge(self, other: "BuildReport") -> None:
        self.created += other.created
        self.reused += other.reused
        self.reconciled += other.reconciled
        self.failed += other.failed

    def summary(self) -> str:
        line = f"created {len(self.created)}, reused {len(self.reused)}"
        if self.reconciled:
            line += f", fixed permissions on {len(self.reconciled)}"
        if self.failed:
            line += f", FAILED {len(self.failed)}"
        return line

    def failure_text(self, limit: int = 8) -> str:
        rows = [f"• {what} — {kind}: {detail}" for what, kind, detail in self.failed[:limit]]
        if len(self.failed) > limit:
            rows.append(f"…and {len(self.failed) - limit} more (see logs)")
        return "\n".join(rows)


class BuildIncomplete(RuntimeError):
    def __init__(self, report: BuildReport, message: str = "") -> None:
        self.report = report
        super().__init__(message or f"Build incomplete: {report.summary()}")


async def _step(report: BuildReport, what: str, factory):
    """Run one setup step. Failures are recorded (not swallowed, not retried forever) and the
    workflow carries on with independent steps. Repeated permission errors or a Cloudflare
    block abort the whole job instead of grinding through every remaining request."""
    try:
        result = await factory()
        report._perm_streak = 0
        return result
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        kind = classify(exc)
        report.failed.append((what, kind.value, describe(exc)))
        log.warning("Setup step failed: %s (%s): %s", what, kind.value, describe(exc))
        if kind is Kind.PERMISSION:
            report._perm_streak += 1
            if report._perm_streak >= 3:
                raise BuildIncomplete(
                    report, "KAMLA is missing permissions (3 consecutive permission errors). "
                            "Give KAMLA the Administrator permission and place its role above the others.")
        else:
            report._perm_streak = 0
        if kind is Kind.GLOBAL_BLOCK:
            raise BuildIncomplete(report, "Discord is temporarily blocking this bot's API access (HTTP 429 without "
                                          "rate-limit headers). Stopped to avoid extending the block; retry later.")
        return None


# ── Idempotent resource helpers ───────────────────────────────────────────────
# Matching rule (deterministic): exact, case-sensitive name AND same channel type AND
# same parent category (None for top-level). Similar-looking names are never reused.

def _overwrites_equal(current: dict, desired: dict) -> bool:
    if set(current) != set(desired):
        return False
    return all(current[k] == desired[k] for k in desired)


def _pick(matches: list, what: str):
    if len(matches) > 1:
        matches = sorted(matches, key=lambda c: (c.position, c.id))
        log.warning("Found %d existing resources matching %s; reusing the first, leaving the rest untouched.",
                    len(matches), what)
    return matches[0]


async def _reconcile(channel, overwrites: dict, report: BuildReport, what: str, user_limit: int | None = None) -> None:
    changes = {}
    if overwrites is not None and not _overwrites_equal(channel.overwrites, overwrites):
        changes["overwrites"] = overwrites
    if user_limit is not None and getattr(channel, "user_limit", user_limit) != user_limit:
        changes["user_limit"] = user_limit
    if changes:
        await bulk.run(lambda: channel.edit(reason="KAMLA setup reconcile", **changes), label=f"reconcile {what}")
        report.reconciled.append(what)


async def _ensure_category(guild, name, overwrites, report):
    matches = [c for c in guild.categories if c.name == name]
    if matches:
        cat = _pick(matches, f"category {name!r}")
        await _reconcile(cat, overwrites, report, f"category {name}")
        report.reused.append(f"category {name}")
        return cat, False
    cat = await bulk.run(lambda: guild.create_category(name, overwrites=overwrites, reason="KAMLA tournament setup"),
                         label=f"create category {name}")
    report.created.append(f"category {name}")
    return cat, True


async def _ensure_text(guild, name, category, overwrites, report):
    pool = category.text_channels if category else [c for c in guild.text_channels if c.category_id is None]
    matches = [c for c in pool if c.name == name]
    where = f"#{name}" + (f" in {category.name}" if category else "")
    if matches:
        ch = _pick(matches, where)
        await _reconcile(ch, overwrites, report, where)
        report.reused.append(where)
        return ch, False
    kwargs = {"overwrites": overwrites, "reason": "KAMLA tournament setup"}
    if category is not None:
        kwargs["category"] = category
    ch = await bulk.run(lambda: guild.create_text_channel(name, **kwargs), label=f"create {where}")
    report.created.append(where)
    return ch, True


async def _ensure_voice(guild, name, category, overwrites, report, user_limit=None):
    pool = category.voice_channels if category else [c for c in guild.voice_channels if c.category_id is None]
    matches = [c for c in pool if c.name == name]
    where = f"🔊{name}" + (f" in {category.name}" if category else "")
    if matches:
        ch = _pick(matches, where)
        await _reconcile(ch, overwrites, report, where, user_limit)
        report.reused.append(where)
        return ch, False
    kwargs = {"overwrites": overwrites, "reason": "KAMLA tournament setup"}
    if category is not None:
        kwargs["category"] = category
    if user_limit:
        kwargs["user_limit"] = user_limit
    ch = await bulk.run(lambda: guild.create_voice_channel(name, **kwargs), label=f"create {where}")
    report.created.append(where)
    return ch, True


async def _send(channel, **kwargs):
    return await bulk.run(lambda: channel.send(**kwargs), label=f"send #{getattr(channel, 'name', '?')}")


async def _bot_already_posted(channel) -> bool:
    """One history request, used only when a channel was REUSED, so a resumed/re-run build
    does not post duplicate panels. Freshly created channels are known to be empty."""
    me_id = channel.guild.me.id

    async def look() -> bool:
        async for m in channel.history(limit=10):
            if m.author.id == me_id:
                return True
        return False
    try:
        return await bulk.run(look, label=f"check #{channel.name}")
    except Exception as exc:  # noqa: BLE001 - when unsure, prefer not to double-post
        log.warning("Could not inspect #%s (%s); assuming content is already there.", channel.name, describe(exc))
        return True


async def _post_if_needed(channel_and_created, poster, report: BuildReport, what: str) -> None:
    if channel_and_created is None:
        return
    channel, created = channel_and_created
    if not created and await _bot_already_posted(channel):
        return
    await _step(report, f"post {what}", poster)


# ── Wipe ──────────────────────────────────────────────────────────────────────

async def wipe_server(guild: discord.Guild) -> None:
    """
    Completely wipe the server — all channels, categories, and roles.
    Keeps only: kamla-config channel, @everyone, and the bot's own managed role.
    Deletions are paced; an already-deleted resource counts as success, so a retry after
    an interruption simply continues. Raises BuildIncomplete (with an accurate report) if
    something could not be deleted, instead of silently building on top of leftovers.
    """
    report = BuildReport()
    async with _marked_building(guild):
        for channel in list(guild.channels):
            if "kamla-config" in channel.name:
                continue
            async def delete_channel(ch=channel):
                try:
                    await bulk.run(lambda: ch.delete(reason="KAMLA server wipe"), label=f"delete {ch.name}")
                except discord.NotFound:
                    pass
            await _step(report, f"delete channel {channel.name}", delete_channel)

        bot_managed_ids = {r.id for r in guild.me.roles if r.managed}
        protected = {guild.default_role.id} | bot_managed_ids
        for role in sorted(guild.roles, key=lambda r: r.position):
            if role.id in protected:
                continue
            async def delete_role(r=role):
                try:
                    await bulk.run(lambda: r.delete(reason="KAMLA server wipe"), label=f"delete role {r.name}")
                except discord.NotFound:
                    pass
            await _step(report, f"delete role {role.name}", delete_role)

    if not report.ok:
        raise BuildIncomplete(report, "Could not completely wipe the server.")
    log.info("Wipe finished for guild %s.", guild.id)


async def _create_roles(guild: discord.Guild, report: BuildReport | None = None) -> dict[str, discord.Role]:
    """Create (or reuse by exact name) every KAMLA role. A missing role would silently produce
    channels without that role's permissions, so any failure here aborts the build."""
    report = report if report is not None else BuildReport()
    roles: dict[str, discord.Role] = {}
    existing = {r.name: r for r in guild.roles}
    for cfg in reversed(ROLE_CONFIG):
        name = cfg["name"]
        if name in existing:
            roles[name] = existing[name]
            report.reused.append(f"role {name}")
            continue

        async def make(cfg=cfg, name=name):
            if cfg["administrator"]:
                perms = discord.Permissions(administrator=True)
            else:
                perms = discord.Permissions(
                    view_channel=True, send_messages=True, read_message_history=True,
                    connect=True, speak=True, stream=True, use_voice_activation=True,
                    embed_links=True, attach_files=True, add_reactions=True,
                )
            role = await bulk.run(
                lambda: guild.create_role(name=name, color=cfg["color"], hoist=cfg["hoist"],
                                          permissions=perms, reason="KAMLA tournament setup"),
                label=f"create role {name}")
            roles[name] = role
            report.created.append(f"role {name}")
        await _step(report, f"create role {name}", make)

    missing = [c["name"] for c in ROLE_CONFIG if c["name"] not in roles]
    if missing:
        raise BuildIncomplete(report, f"Could not create required role(s): {', '.join(missing)}.")
    return roles


def _bot_ow(guild):
    return _allow("view_channel", "send_messages", "read_message_history",
                  "manage_messages", "embed_links", "attach_files",
                  "connect", "speak", "move_members")


def _build_orgcom_overwrites(guild, roles):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in ORG_ACCESS_ROLES:
        if rn in roles:
            ow[roles[rn]] = _role_ow(admin=True, view=True, send=True)
    for rn in KAMLA_ROLE_NAMES:
        if rn in ORG_ACCESS_ROLES:
            continue
        if rn in roles:
            ow[roles[rn]] = discord.PermissionOverwrite(view_channel=False)
    return ow


def _build_all_role_overwrites(guild, roles, send=True):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn in roles:
            ow[roles[rn]] = _role_ow(
                admin=rn in ADMIN_ROLE_NAMES,
                view=True,
                send=send or rn in ADMIN_ROLE_NAMES,
            )
    return ow


def _build_announce_overwrites(guild, roles):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn in roles:
            is_admin = rn in ADMIN_ROLE_NAMES
            ow[roles[rn]] = discord.PermissionOverwrite(
                view_channel=True, read_message_history=True,
                send_messages=is_admin, manage_messages=is_admin,
                embed_links=is_admin, attach_files=is_admin,
            )
    return ow


def _build_public_overwrites(guild):
    return {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=False, read_message_history=True,
        ),
        guild.me: _bot_ow(guild),
    }


def _build_play_overwrites(guild, roles):
    """PLAY category: every KAMLA role except VISITOR can view/use it."""
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn == "VISITOR":
            if rn in roles:
                ow[roles[rn]] = discord.PermissionOverwrite(view_channel=False)
            continue
        if rn in roles:
            ow[roles[rn]] = _role_ow(admin=rn in ADMIN_ROLE_NAMES, view=True, send=True)
    return ow


def _build_toss_overwrites(guild, roles):
    """toss-coin channel: visible to everyone but VISITOR; only CAP/TABBY/ORG (+admins) may send."""
    toss_allowed = {"CAP", "TABBY", "ORG"}
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn == "VISITOR":
            if rn in roles:
                ow[roles[rn]] = discord.PermissionOverwrite(view_channel=False)
            continue
        if rn in roles:
            can_send = rn in toss_allowed or rn in ADMIN_ROLE_NAMES
            ow[roles[rn]] = discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=can_send,
            )
    return ow


def _room_category_ow(guild, roles):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn in roles:
            ow[roles[rn]] = discord.PermissionOverwrite(
                view_channel=True, read_message_history=True,
            )
    return ow


def _room_text_ow(guild, roles, *, hide_from=(), allow_send=True):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn not in roles:
            continue
        if rn in hide_from:
            ow[roles[rn]] = discord.PermissionOverwrite(view_channel=False)
        else:
            is_admin = rn in ADMIN_ROLE_NAMES
            ow[roles[rn]] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=allow_send or is_admin,
                embed_links=True,
                attach_files=True,
            )
    return ow


def _room_voice_ow(guild, roles, *, hide_from=(), no_speak=()):
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_ow(guild),
    }
    for rn in KAMLA_ROLE_NAMES:
        if rn not in roles:
            continue
        if rn in hide_from:
            ow[roles[rn]] = discord.PermissionOverwrite(view_channel=False, connect=False)
            continue
        can_speak = rn not in no_speak
        ow[roles[rn]] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=can_speak,
            use_voice_activation=can_speak,
            stream=can_speak,
        )
    return ow


def build_params(setup_data: dict) -> dict:
    return {
        "format": str(setup_data["format"]).lower(),
        "rooms": int(setup_data["rooms"]),
        "tournament_name": setup_data["tournament_name"],
        "timezone": setup_data["timezone"],
    }


async def find_resumable(guild: discord.Guild, setup_data: dict) -> bool:
    """True when an earlier build with IDENTICAL settings was interrupted after the wipe.
    Resuming reconciles the existing structure instead of wiping and starting from zero."""
    cfg = await config_manager.get_config(guild)
    pending = cfg.get("pending_build") if cfg else None
    return bool(pending and pending.get("wiped") and pending.get("params") == build_params(setup_data))


async def mark_build_state(guild: discord.Guild, setup_data: dict, *, wiped: bool) -> None:
    await config_manager.update_config(
        guild, pending_build={"params": build_params(setup_data), "wiped": wiped}
    )


async def build_server(guild, fmt, rooms, timezone, tournament_name, creator_id, progress=None) -> BuildReport:
    """Idempotent: existing matching resources are reused, so running it again (or resuming
    after a failure) reconciles instead of duplicating. Raises BuildIncomplete when something
    could not be created; everything that did succeed is kept and reused on the next run."""
    async with _marked_building(guild):
        return await _build_server_inner(guild, fmt, rooms, timezone, tournament_name, creator_id, progress)


async def _tell(progress, text: str) -> None:
    if progress is not None:
        try:
            await progress(text)
        except Exception as exc:  # noqa: BLE001 - progress display must never break a build
            log.debug("progress callback failed: %s", describe(exc))


async def _ensure_tree(guild, report, cat_name, cat_ow, entries):
    """Create/reuse a category and its children. entries: (kind, name, overwrites[, user_limit])."""
    got = await _step(report, f"category {cat_name}", lambda: _ensure_category(guild, cat_name, cat_ow, report))
    if got is None:
        return None, {}
    cat, _ = got
    children = {}
    for entry in entries:
        kind, name, ow = entry[0], entry[1], entry[2]
        if kind == "text":
            children[name] = await _step(report, f"#{name}", lambda n=name, o=ow: _ensure_text(guild, n, cat, o, report))
        else:
            limit = entry[3] if len(entry) > 3 else None
            children[name] = await _step(report, f"🔊{name}", lambda n=name, o=ow, l=limit: _ensure_voice(guild, n, cat, o, report, l))
    return cat, children


async def _build_server_inner(
    guild: discord.Guild,
    fmt: str,
    rooms: int,
    timezone: str,
    tournament_name: str,
    creator_id: int,
    progress=None,
) -> BuildReport:
    report = BuildReport()
    await _tell(progress, "Creating roles…")
    roles = await _create_roles(guild, report)

    await bulk.best_effort(lambda: guild.edit(name=tournament_name, reason="KAMLA tournament setup"),
                           label="rename server")
    await bulk.best_effort(lambda: guild.me.edit(nick=tournament_name[:32]), label="set bot nickname")

    await _tell(progress, "Creating core channels…")
    pub_ow = _build_public_overwrites(guild)
    top = {}
    for name in TOP_LEVEL_CHANNELS:
        top[name] = await _step(report, f"#{name}", lambda n=name: _ensure_text(guild, n, None, pub_ow, report))

    orgcom_ow = _build_orgcom_overwrites(guild, roles)
    _, orgcom = await _ensure_tree(guild, report, "⚜️︱ORGCOM", orgcom_ow, [
        ("text", "⚙️︱settings", orgcom_ow), ("text", "🏴︱org", orgcom_ow), ("text", "📂︱document", orgcom_ow),
        ("voice", "ORG", orgcom_ow), ("voice", "Control Room", orgcom_ow),
    ])

    assign_ow = _build_orgcom_overwrites(guild, roles)
    await _ensure_tree(guild, report, "🛅︱ASSIGN", assign_ow, [
        ("text", rn.lower().replace(" ", "-"), assign_ow) for rn in ASSIGN_ROLE_ORDER
    ])

    ga_all_ow = _build_all_role_overwrites(guild, roles, send=True)
    ga_ann_ow = _build_announce_overwrites(guild, roles)
    ga_entries = [("text", "📨︱ga-text", ga_all_ow)]
    ga_entries += [("text", n, ga_ann_ow) for n in GA_ANNOUNCE_CHANNELS]
    ga_entries += [("voice", "🏟️︱GRAND AUDITORIUM", _build_all_role_overwrites(guild, roles)),
                   ("voice", "😤︱CLASH-EQUITY ROOM", _build_all_role_overwrites(guild, roles))]
    await _ensure_tree(guild, report, "🏟️︱GRAND AUDITORIUM", _build_all_role_overwrites(guild, roles), ga_entries)

    play_ow = _build_play_overwrites(guild, roles)
    await _ensure_tree(guild, report, "🎮︱PLAY", play_ow, [
        ("text", "❌︱tic-tac-toe︱⭕", play_ow),
        ("text", "🤛︱rock✊-paper📰-scissors✌️", play_ow),
        ("text", "🪙︱toss-coin", _build_toss_overwrites(guild, roles)),
    ])

    info_send_ow = _build_announce_overwrites(guild, roles)
    await _ensure_tree(guild, report, "ℹ️︱INFORMATION", _build_all_role_overwrites(guild, roles, send=False),
                       [("text", n, info_send_ow) for n in INFO_CHANNELS])

    settings = orgcom.get("⚙️︱settings")
    previous = config_manager.get_cached(guild.id) or {}
    cfg_data = {
        "format":              fmt,
        "rooms":               rooms,
        "timezone":            timezone,
        "tournament_name":     tournament_name,
        "created_by":          creator_id,
        "created_at":          discord.utils.utcnow().isoformat(),
        "settings_channel_id": settings[0].id if settings else None,
        "locked":              False,
    }
    if previous.get("pending_build"):
        cfg_data["pending_build"] = previous["pending_build"]      # keep the resume marker until we finish
    await config_manager.set_config(guild, cfg_data)

    await _tell(progress, "Posting guide messages…")
    await _post_if_needed(top.get("👐🏻︱meet-the-developer"), lambda: _post_meet_developer(top["👐🏻︱meet-the-developer"][0]), report, "meet-the-developer")
    await _post_if_needed(top.get("👋🏻︱welcome"), lambda: _post_welcome(guild), report, "welcome")
    await _post_if_needed(top.get("❓︱get-role"), lambda: _post_get_role(guild), report, "get-role")
    await _post_if_needed(top.get("🦧︱how-to-use-this-server"), lambda: _post_how_to_use(guild), report, "how-to-use")
    if settings:
        await _post_if_needed(settings, lambda: _post_settings_panel(settings[0], cfg_data), report, "settings panel")

    await _create_rooms(guild, roles, fmt, rooms, start_index=1, report=report, progress=progress)

    if not report.ok:
        raise BuildIncomplete(report)
    cfg_data.pop("pending_build", None)
    await config_manager.set_config(guild, cfg_data)       # clears the resume marker
    return report


AP_PREP_NAMES = {"GOVT PREP", "OPP PREP"}
BP_PREP_NAMES = {"OG PREP", "OO PREP", "CG PREP", "CO PREP"}
ROOM_CATEGORY_RE = re.compile(r"^ROOM (?P<number>\d{2})$")


async def switch_format_rooms(guild: discord.Guild, new_fmt: str, rooms: int) -> BuildReport:
    """Re-lay the prep/adjudication channels of every room for `new_fmt`. Rooms already in the
    target layout are left alone (idempotent); an interrupted run continues where it stopped."""
    report = BuildReport()
    roles = {r.name: r for r in guild.roles}
    prep_ow = _room_voice_ow(guild, roles, hide_from=("VISITOR",))
    adj_ow  = _room_voice_ow(guild, roles, hide_from=("DEBATER", "VISITOR"))
    all_prep = AP_PREP_NAMES | BP_PREP_NAMES
    fmt = new_fmt.lower()
    wanted = ([("OG PREP", 2), ("OO PREP", 2), ("CG PREP", 2), ("CO PREP", 2)] if fmt == "bp"
              else [("GOVT PREP", 3), ("OPP PREP", 3)])
    wanted_names = {n for n, _ in wanted}

    async with _marked_building(guild):
        for i in range(1, rooms + 1):
            cat = discord.utils.get(guild.categories, name=f"ROOM {i:02d}")
            if not cat:
                continue
            have = {ch.name for ch in cat.voice_channels}
            if (have & all_prep) == wanted_names and "ADJUDICATION" in have:
                report.reused.append(f"room {i:02d} layout")
                continue

            for ch in list(cat.voice_channels):
                if ch.name in (all_prep - wanted_names) or ch.name == "ADJUDICATION":
                    async def drop(c=ch):
                        try:
                            await bulk.run(lambda: c.delete(reason="KAMLA format switch"), label=f"delete {c.name}")
                        except discord.NotFound:
                            pass
                    await _step(report, f"delete {ch.name} in ROOM {i:02d}", drop)

            for prep_name, limit in wanted:
                await _step(report, f"{prep_name} in ROOM {i:02d}",
                            lambda n=prep_name, l=limit, c=cat: _ensure_voice(guild, n, c, prep_ow, report, l))
            await _step(report, f"ADJUDICATION in ROOM {i:02d}",
                        lambda c=cat: _ensure_voice(guild, "ADJUDICATION", c, adj_ow, report))

    if not report.ok:
        raise BuildIncomplete(report)
    return report


async def _create_rooms(
    guild: discord.Guild,
    roles: dict,
    fmt: str,
    count: int,
    start_index: int = 1,
    report: BuildReport | None = None,
    progress=None,
) -> BuildReport:
    """Create (or complete) ROOM categories. Existing, exactly-named channels are reused, so
    this is safe to re-run. Raises BuildIncomplete only if called standalone (report=None)
    and something failed; when a report is passed in, the caller decides."""
    standalone = report is None
    report = report if report is not None else BuildReport()
    cat_ow    = _room_category_ow(guild, roles)
    text_ow   = _room_text_ow(guild, roles, hide_from=("VISITOR",), allow_send=True)
    allin_ow  = _room_text_ow(guild, roles, hide_from=("DEBATER", "VISITOR"), allow_send=False)
    debate_ow = _room_voice_ow(guild, roles, no_speak=("VISITOR",))
    prep_ow   = _room_voice_ow(guild, roles, hide_from=("VISITOR",))
    adj_ow    = _room_voice_ow(guild, roles, hide_from=("DEBATER", "VISITOR"))

    if fmt.lower() == "ap":
        preps = [("GOVT PREP", 3), ("OPP PREP", 3)]
    else:
        preps = [("OG PREP", 2), ("OO PREP", 2), ("CG PREP", 2), ("CO PREP", 2)]

    for i in range(start_index, start_index + count):
        await _tell(progress, f"Creating rooms… {i - start_index + 1}/{count}")
        room_name = f"ROOM {i:02d}"
        entries = [("text", "timer", text_ow), ("text", "poi", text_ow), ("text", "all-in", allin_ow),
                   ("voice", "DEBATE ROOM", debate_ow)]
        entries += [("voice", n, prep_ow, limit) for n, limit in preps]
        entries += [("voice", "ADJUDICATION", adj_ow)]
        _, children = await _ensure_tree(guild, report, room_name, cat_ow, entries)

        all_in = children.get("all-in")
        if all_in:
            await _post_if_needed(all_in, lambda ch=all_in[0]: _post_all_in_panel(ch), report, f"{room_name} all-in panel")

    if standalone and not report.ok:
        raise BuildIncomplete(report)
    return report


async def _post_all_in_panel(channel: discord.TextChannel) -> None:
    from allin_cog import AllInView
    await _send(
        channel,
        embed=discord.Embed(
            title="🔴 Push Back All",
            description="Press the button below to move everyone from prep rooms back to **DEBATE ROOM**.\n\n"
                        "⛔ Text messages are not allowed in this channel.",
            color=discord.Color.red(),
        ),
        view=AllInView(),
    )


async def remove_room(guild: discord.Guild, index: int) -> BuildReport:
    """Delete ROOM {index} (children first). Already-missing resources count as success."""
    report = BuildReport()
    cat = discord.utils.get(guild.categories, name=f"ROOM {index:02d}")
    if not cat:
        return report
    async with _marked_building(guild):
        for ch in list(cat.channels):
            async def drop(c=ch):
                try:
                    await bulk.run(lambda: c.delete(reason="KAMLA remove room"), label=f"delete {c.name}")
                except discord.NotFound:
                    pass
            await _step(report, f"delete {ch.name}", drop)
        async def drop_cat():
            try:
                await bulk.run(lambda: cat.delete(reason="KAMLA remove room"), label=f"delete {cat.name}")
            except discord.NotFound:
                pass
        await _step(report, f"delete {cat.name}", drop_cat)
    if not report.ok:
        raise BuildIncomplete(report)
    return report


async def _post_meet_developer(channel: discord.TextChannel) -> None:
    embed = discord.Embed(
        title="👐 Meet the Developer",
        description=(
            "KAMLA was built by **Istiack** — a passionate developer and debater.\n\n"
            "Use the buttons below to get in touch, support the project, or create a tournament."
        ),
        color=discord.Color.blurple(),
    )
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Developer Contact", style=discord.ButtonStyle.link,
                                    url="https://istiack.ami.bd/#contact"))
    view.add_item(discord.ui.Button(label="Donate", style=discord.ButtonStyle.link,
                                    url="https://istiack.ami.bd/#donate"))
    view.add_item(discord.ui.Button(label="Create Tournament", style=discord.ButtonStyle.link,
                                    url="https://kamla-bot.pages.dev"))

    import pathlib
    logo_path = pathlib.Path(__file__).parent / "m.png"
    if logo_path.exists():
        embed.set_image(url="attachment://m.png")
        # A discord.File is consumed by one send; build a fresh one per attempt.
        await bulk.run(
            lambda: channel.send(embed=embed, file=discord.File(str(logo_path), filename="m.png"), view=view),
            label="send meet-the-developer",
        )
    else:
        await _send(channel, embed=embed, view=view)


async def _post_welcome(guild: discord.Guild) -> None:
    channel = discord.utils.get(guild.text_channels, name="👋🏻︱welcome")
    if not channel:
        return
    embed = discord.Embed(
        title="👋 Welcome to the Tournament Server!",
        description=(
            "This server is powered by **KAMLA** — an automated tournament management bot.\n\n"
            "**How to get a role:**\n"
            "1. Head to **❓︱get-role**\n"
            "2. Click the button that matches your role\n"
            "3. Your role will be assigned automatically\n\n"
            "**Roles:**\n"
            "🔵 **Invited Adjudicator** — Officially invited judges\n"
            "🟣 **Independent Adjudicator** — Self-registered judges\n"
            "🩵 **Debater** — Competing speakers\n"
            "⬜ **Visitor** — Observers and guests\n\n"
            "Admin roles (ORG/CAP/TABBY/EQUITY) are assigned by organisers only."
        ),
        color=discord.Color.blurple(),
    )
    await _send(channel, embed=embed)


async def _post_get_role(guild: discord.Guild) -> None:
    channel = discord.utils.get(guild.text_channels, name="❓︱get-role")
    if not channel:
        return
    from roles_cog import RoleSelectView
    embed = discord.Embed(
        title="❓ Get Your Role",
        description=(
            "Click a button below to claim your role.\n\n"
            "You may only hold **one role** at a time — your previous role will be removed automatically.\n\n"
            "⚠️ If you are **ORG / CAP / TABBY / EQUITY**, do **not** click here — "
            "contact the tournament ORG to change your role."
        ),
        color=discord.Color.blurple(),
    )
    await _send(channel, embed=embed, view=RoleSelectView())


async def _post_how_to_use(guild: discord.Guild) -> None:
    channel = discord.utils.get(guild.text_channels, name="🦧︱how-to-use-this-server")
    if not channel:
        return
    embed = discord.Embed(title="🦧 How to Use This Server", color=discord.Color.blurple())
    embed.add_field(name="📌 Channels",
        value="• **welcome** — Read this first\n• **get-role** — Claim your role\n"
              "• **GRAND AUDITORIUM** — Main hub\n• **INFORMATION** — Official documents\n"
              "• **ROOM XX** — Individual debate rooms", inline=False)
    embed.add_field(name="⏱️ Timer",
        value="In **#timer**, send a time: `10m` `1h` `15s` `1h10m` `10m30s`\n"
              "Only lowercase d/h/m/s — no symbols or spaces.", inline=False)
    embed.add_field(name="🙋 POI",
        value="In **#poi**, type `poi` (any case) to request a Point of Information.",
        inline=False)
    embed.add_field(name="🔴 All-In",
        value="In **#all-in**, only **ORG / CAP / TABBY / Invited / Independent Adjudicator** "
              "can press **Push Back All** to pull everyone back from prep to the debate room. "
              "Text messages are not allowed.",
        inline=False)
    await _send(channel, embed=embed)


async def _post_settings_panel(channel: discord.TextChannel, cfg: dict) -> None:
    from settings_cog import build_settings_embed, SettingsView
    embed = build_settings_embed(cfg)
    await _send(channel, embed=embed, view=SettingsView())


# ── Channel / Category Restoration ────────────────────────────────────────────

def _room_number(category_name: str) -> int | None:
    match = ROOM_CATEGORY_RE.fullmatch(category_name)
    return int(match.group("number")) if match else None


def _room_channel_names(fmt: str) -> tuple[list[str], list[str]]:
    text = ["timer", "poi", "all-in"]
    prep = ["GOVT PREP", "OPP PREP"] if fmt.lower() == "ap" else [
        "OG PREP", "OO PREP", "CG PREP", "CO PREP"
    ]
    return text, ["DEBATE ROOM", *prep, "ADJUDICATION"]


_guard_locks: dict[int, asyncio.Lock] = {}


def _guard_lock(guild: discord.Guild) -> asyncio.Lock:
    return _guard_locks.setdefault(guild.id, asyncio.Lock())


async def _saved_config(guild: discord.Guild) -> dict:
    cfg = config_manager.get_cached(guild.id)
    if cfg:
        return cfg
    try:
        return await asyncio.wait_for(config_manager.get_config(guild), timeout=3.0)
    except Exception:
        return {}


async def restore_if_protected(
    guild: discord.Guild,
    channel_name: str,
    is_category: bool,
    parent_category_name: str | None = None,
) -> bool:
    """
    Called from on_guild_channel_delete.
    Returns True if the channel/category was protected and has been restored.
    Runs in the background — fire-and-forget via asyncio.create_task.
    Restores for one guild are serialised, so deleting many protected channels at once
    produces a queue of paced restores instead of overlapping bursts.
    """
    async with _guard_lock(guild):
        try:
            return await _restore_if_protected_locked(guild, channel_name, is_category, parent_category_name)
        except Exception as exc:  # noqa: BLE001
            log.error("Restore of %r failed (%s): %s", channel_name, classify(exc).value, describe(exc))
            return False


async def _restore_if_protected_locked(
    guild: discord.Guild,
    channel_name: str,
    is_category: bool,
    parent_category_name: str | None = None,
) -> bool:
    _restoring_guilds.add(guild.id)
    try:
        if is_category:
            if channel_name in PROTECTED_CATEGORIES:
                await asyncio.sleep(1)
                await _restore_category(guild, channel_name)
                await normalize_kamla_order(guild)
                return True

            room_index = _room_number(channel_name)
            cfg = await _saved_config(guild) if room_index else {}
            if room_index and room_index <= int(cfg.get("rooms", 0)):
                await asyncio.sleep(1)
                await _restore_room_category(guild, room_index)
                await normalize_kamla_order(guild)
                return True
            return False

        if channel_name in PROTECTED_TOP_LEVEL_CHANNELS:
            await asyncio.sleep(1)
            await _restore_top_level_channel(guild, channel_name)
            await normalize_kamla_order(guild)
            return True

        # Restore channels inside the fixed KAMLA categories.
        for cat_name, structure in CATEGORY_CHANNEL_STRUCTURE.items():
            if channel_name in structure["text"] or channel_name in structure["voice"]:
                cat = discord.utils.get(guild.categories, name=cat_name)
                if cat is None:
                    return False
                await asyncio.sleep(1)
                roles = {r.name: r for r in guild.roles}
                is_voice = channel_name in structure["voice"]
                await _restore_single_channel(guild, roles, cat, cat_name, channel_name, is_voice)
                await normalize_kamla_order(guild)
                return True

        # Restore a missing channel inside ROOM XX.
        room_index = _room_number(parent_category_name or "")
        cfg = await _saved_config(guild) if room_index else {}
        if room_index and room_index <= int(cfg.get("rooms", 0)):
            text_names, voice_names = _room_channel_names(cfg.get("format", "ap"))
            if channel_name in text_names or channel_name in voice_names:
                cat = discord.utils.get(guild.categories, name=parent_category_name)
                if cat is None:
                    await _restore_room_category(guild, room_index)
                else:
                    roles = {r.name: r for r in guild.roles}
                    await _restore_room_channel(
                        guild, roles, cat, channel_name, cfg.get("format", "ap")
                    )
                await normalize_kamla_order(guild)
                return True
        return False
    finally:
        _restoring_guilds.discard(guild.id)


async def _restore_category(guild: discord.Guild, cat_name: str) -> None:
    """Recreate a protected category and ALL its missing channels."""
    roles = {r.name: r for r in guild.roles}

    # Compute the right overwrites for this category
    ow = _category_overwrites(guild, roles, cat_name)

    # Check if it already got recreated while we were sleeping
    existing_cat = discord.utils.get(guild.categories, name=cat_name)
    if existing_cat is None:
        try:
            existing_cat = await bulk.run(lambda: guild.create_category(cat_name, overwrites=ow),
                                          label=f"restore category {cat_name}")
            log.info("Recreated category: %s", cat_name)
        except Exception as e:
            log.error("Failed to recreate category %s: %s", cat_name, describe(e))
            return

    structure = CATEGORY_CHANNEL_STRUCTURE.get(cat_name, {"text": [], "voice": []})
    existing_names = {ch.name for ch in existing_cat.channels}

    for ch_name in structure["text"]:
        if ch_name not in existing_names:
            await _restore_single_channel(guild, roles, existing_cat, cat_name, ch_name, False)
            await asyncio.sleep(0.3)

    for ch_name in structure["voice"]:
        if ch_name not in existing_names:
            await _restore_single_channel(guild, roles, existing_cat, cat_name, ch_name, True)
            await asyncio.sleep(0.3)

    # Re-post content for channels that need it
    await _repost_channel_content(guild, cat_name, existing_cat)


async def _restore_top_level_channel(guild: discord.Guild, ch_name: str) -> None:
    """Recreate a protected top-level (no-category) channel."""
    existing = discord.utils.get(guild.text_channels, name=ch_name)
    if existing:
        return  # already back

    pub_ow = _build_public_overwrites(guild)
    try:
        new_ch = await bulk.run(lambda: guild.create_text_channel(ch_name, overwrites=pub_ow),
                                label=f"restore {ch_name}")
        log.info("Recreated top-level channel: %s", ch_name)
    except Exception as e:
        log.error("Failed to recreate channel %s: %s", ch_name, describe(e))
        return

    # Re-post embedded content
    if ch_name == "👐🏻︱meet-the-developer":
        await _post_meet_developer(new_ch)
    elif ch_name == "👋🏻︱welcome":
        await _post_welcome(guild)
    elif ch_name == "❓︱get-role":
        await _post_get_role(guild)
    elif ch_name == "🦧︱how-to-use-this-server":
        await _post_how_to_use(guild)


async def _restore_single_channel(
    guild: discord.Guild,
    roles: dict,
    cat: discord.CategoryChannel,
    cat_name: str,
    ch_name: str,
    is_voice: bool,
) -> None:
    """Recreate one channel inside a protected category."""
    existing = discord.utils.get(
        cat.voice_channels if is_voice else cat.text_channels, name=ch_name
    )
    if existing:
        return  # already back

    ow = _channel_overwrites(guild, roles, cat_name, ch_name)
    try:
        if is_voice:
            await bulk.run(lambda: guild.create_voice_channel(ch_name, category=cat, overwrites=ow),
                           label=f"restore {ch_name}")
        else:
            new_ch = await bulk.run(lambda: guild.create_text_channel(ch_name, category=cat, overwrites=ow),
                                    label=f"restore {ch_name}")
            # Re-post settings panel if it's the settings channel
            if ch_name == "⚙️︱settings":
                from config import config_manager
                cfg = config_manager.get_cached(guild.id) or {}
                if cfg:
                    await _post_settings_panel(new_ch, cfg)
        log.info("Recreated channel #%s in %s", ch_name, cat_name)
    except Exception as e:
        log.error("Failed to recreate channel %s: %s", ch_name, describe(e))


async def _restore_room_category(guild: discord.Guild, room_index: int) -> None:
    """Recreate one ROOM category with the saved AP/BP channel layout."""
    cfg = await _saved_config(guild)
    fmt = cfg.get("format", "ap")
    roles = {r.name: r for r in guild.roles}
    room_name = f"ROOM {room_index:02d}"

    existing = discord.utils.get(guild.categories, name=room_name)
    if existing is None:
        try:
            await _create_rooms(guild, roles, fmt, 1, start_index=room_index)
            log.info("Recreated category: %s", room_name)
        except BuildIncomplete as e:
            log.error("Could not fully recreate %s: %s", room_name, e.report.summary())
        return

    text_names, voice_names = _room_channel_names(fmt)
    for channel_name in text_names:
        await _restore_room_channel(guild, roles, existing, channel_name, fmt)
        await asyncio.sleep(0.2)
    for channel_name in voice_names:
        await _restore_room_channel(guild, roles, existing, channel_name, fmt)
        await asyncio.sleep(0.2)


async def _restore_room_channel(
    guild: discord.Guild,
    roles: dict,
    category: discord.CategoryChannel,
    channel_name: str,
    fmt: str,
) -> None:
    """Recreate one missing channel in a ROOM category."""
    text_names, voice_names = _room_channel_names(fmt)
    is_voice = channel_name in voice_names
    existing = discord.utils.get(
        category.voice_channels if is_voice else category.text_channels,
        name=channel_name,
    )
    if existing:
        return

    if channel_name == "all-in":
        overwrites = _room_text_ow(
            guild, roles, hide_from=("DEBATER", "VISITOR"), allow_send=False
        )
    elif is_voice:
        if channel_name == "DEBATE ROOM":
            overwrites = _room_voice_ow(guild, roles, no_speak=("VISITOR",))
        elif channel_name == "ADJUDICATION":
            overwrites = _room_voice_ow(
                guild, roles, hide_from=("DEBATER", "VISITOR")
            )
        else:
            overwrites = _room_voice_ow(guild, roles, hide_from=("VISITOR",))
    else:
        overwrites = _room_text_ow(
            guild, roles, hide_from=("VISITOR",), allow_send=True
        )

    try:
        if is_voice:
            user_limit = 3 if fmt.lower() == "ap" else 2
            await bulk.run(
                lambda: guild.create_voice_channel(
                    channel_name,
                    category=category,
                    overwrites=overwrites,
                    user_limit=user_limit if "PREP" in channel_name else 0,
                ),
                label=f"restore {channel_name}",
            )
        else:
            new_channel = await bulk.run(
                lambda: guild.create_text_channel(channel_name, category=category, overwrites=overwrites),
                label=f"restore {channel_name}",
            )
            if channel_name == "all-in":
                await _post_all_in_panel(new_channel)
        log.info("Recreated channel #%s in %s", channel_name, category.name)
    except Exception as e:
        log.error("Failed to recreate room channel %s: %s", channel_name, describe(e))


_normalizing: set[int] = set()
_normalize_dirty: set[int] = set()


async def normalize_kamla_order(guild: discord.Guild) -> None:
    """
    Put KAMLA's categories, room categories, and their child channels back into
    the canonical order. User-created channels/categories are left untouched.

    Cheap when nothing is wrong: the current order is compared with the desired one
    first and NO request is made if it already matches (previously every call issued a
    position edit for every category and channel — hundreds of requests per restore).
    Concurrent calls for one guild collapse into a single follow-up pass.
    """
    if guild.id in _normalizing:
        _normalize_dirty.add(guild.id)
        return
    _normalizing.add(guild.id)
    try:
        while True:
            _normalize_dirty.discard(guild.id)
            await _normalize_once(guild)
            if guild.id not in _normalize_dirty:
                break
    finally:
        _normalizing.discard(guild.id)


async def _normalize_once(guild: discord.Guild) -> None:
    desired_category_names = [
        "⚜️︱ORGCOM",
        "🛅︱ASSIGN",
        "🏟️︱GRAND AUDITORIUM",
        "🎮︱PLAY",
        "ℹ️︱INFORMATION",
    ]
    rooms = sorted(
        (
            (room_index, category)
            for category in guild.categories
            if (room_index := _room_number(category.name)) is not None
        ),
        key=lambda item: item[0],
    )
    desired_categories: list[discord.CategoryChannel] = []
    for name in desired_category_names:
        category = discord.utils.get(guild.categories, name=name)
        if category:
            desired_categories.append(category)
    desired_categories.extend(category for _, category in rooms)

    current_top = sorted(guild.categories, key=lambda c: (c.position, c.id))[:len(desired_categories)]
    if [c.id for c in current_top] != [c.id for c in desired_categories]:
        # Discord positions are global; moving in reverse order avoids later moves
        # shifting already placed categories.
        for position, category in reversed(list(enumerate(desired_categories))):
            await bulk.best_effort(
                lambda c=category, p=position: c.edit(position=p, reason="KAMLA canonical channel order"),
                label="reorder category")

    cfg = None
    for category in desired_categories:
        if category.name in CATEGORY_CHANNEL_STRUCTURE:
            structure = CATEGORY_CHANNEL_STRUCTURE[category.name]
            ordered_names = [*structure["text"], *structure["voice"]]
        else:
            if cfg is None:
                cfg = await _saved_config(guild)
            text_names, voice_names = _room_channel_names(cfg.get("format", "ap"))
            ordered_names = [*text_names, *voice_names]

        current = {channel.name: channel for channel in category.channels}
        ordered_channels = [current[name] for name in ordered_names if name in current]
        actual = sorted(category.channels, key=lambda c: (c.position, c.id))[:len(ordered_channels)]
        if [c.id for c in actual] == [c.id for c in ordered_channels]:
            continue
        for position, channel in reversed(list(enumerate(ordered_channels))):
            await bulk.best_effort(
                lambda c=channel, p=position: c.edit(position=p, reason="KAMLA canonical channel order"),
                label="reorder channel")


def _category_overwrites(guild: discord.Guild, roles: dict, cat_name: str) -> dict:
    if cat_name in ("⚜️︱ORGCOM", "🛅︱ASSIGN"):
        return _build_orgcom_overwrites(guild, roles)
    elif cat_name == "🏟️︱GRAND AUDITORIUM":
        return _build_all_role_overwrites(guild, roles)
    elif cat_name == "🎮︱PLAY":
        return _build_play_overwrites(guild, roles)
    elif cat_name == "ℹ️︱INFORMATION":
        return _build_all_role_overwrites(guild, roles, send=False)
    return {}


def _channel_overwrites(guild: discord.Guild, roles: dict, cat_name: str, ch_name: str) -> dict:
    if cat_name in ("⚜️︱ORGCOM", "🛅︱ASSIGN"):
        return _build_orgcom_overwrites(guild, roles)
    elif cat_name == "🏟️︱GRAND AUDITORIUM":
        announce_channels = ["🎓︱motion", "🎓︱announcement", "🎓︱break",
                             "🎓︱matchup", "🎓︱ballot", "📊︱poll", "📊︱yes-no-voting"]
        if ch_name in announce_channels:
            return _build_announce_overwrites(guild, roles)
        return _build_all_role_overwrites(guild, roles, send=True)
    elif cat_name == "🎮︱PLAY":
        if ch_name == "🪙︱toss-coin":
            return _build_toss_overwrites(guild, roles)
        return _build_play_overwrites(guild, roles)
    elif cat_name == "ℹ️︱INFORMATION":
        return _build_announce_overwrites(guild, roles)
    return {}


async def _repost_channel_content(
    guild: discord.Guild,
    cat_name: str,
    cat: discord.CategoryChannel,
) -> None:
    """Re-post embedded bot messages in channels that need them after restoration."""
    if cat_name == "🛅︱ASSIGN":
        pass  # Assign channels don't need initial content
    elif cat_name == "⚜️︱ORGCOM":
        settings_ch = discord.utils.get(cat.text_channels, name="⚙️︱settings")
        if settings_ch:
            from config import config_manager
            cfg = config_manager.get_cached(guild.id) or {}
            if cfg:
                try:
                    # Only post if the channel is empty
                    history = [m async for m in settings_ch.history(limit=1)]
                    if not history:
                        await _post_settings_panel(settings_ch, cfg)
                except Exception:
                    pass

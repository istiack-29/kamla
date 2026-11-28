"""installer.py — what happens when KAMLA is added to a server, and what `/st` does.

On join KAMLA immediately creates two channels:
  ⚙️︱kamla-config — hidden, read-only configuration storage;
  📊︱server-status — visible ONLY to the installer and KAMLA. It shows "I'M HERE", the compatibility check and,
                    when safe, a Create Now button. `/st` works only here and only for the installer.

Nothing here deletes anything. A server that already holds other people's channels or roles is declared "not a fresh
server" and no setup is offered. Installation is idempotent and resumable: every resource is looked up by its exact
name first, so running it again creates only what is missing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import discord

import naming
from config import config_manager, KAMLA_ROLE_NAMES, kamla_role, with_thread_lock
from naming import CH_CONFIG, CH_STATUS, CAT_ORGCOM, CAT_GA, CAT_PLAY, CAT_INFO, CAT_SECURITY, ROOM_CATEGORY_RE
from ratelimit import bulk, describe, edits

log = logging.getLogger("kamla.installer")

# What KAMLA needs. Administrator covers everything; otherwise each of these must be present.
REQUIRED_PERMISSIONS = (
    "view_channel", "send_messages", "embed_links", "read_message_history", "manage_channels", "manage_roles",
    "manage_messages", "manage_threads", "create_instant_invite", "manage_guild", "move_members", "kick_members",
    "view_audit_log",
)
# Channels / categories a brand-new Discord server comes with.
DEFAULT_NAMES = frozenset({"general", "General", "Text Channels", "Voice Channels"})
REMINDER_INTERVAL = 300.0           # hierarchy reminders: at most one per 5 minutes
_last_reminder: dict[int, float] = {}
_clock = time.monotonic


def kamla_managed_names() -> set[str]:
    """Every channel/category name KAMLA itself creates (rooms are matched by pattern)."""
    names = {CH_CONFIG, CH_STATUS, naming.CH_DEVELOPERS, naming.CH_MEMBERS, naming.CH_GET_ROLE, naming.CH_HOW_TO,
             naming.CH_LINKS}
    from server_builder import CATEGORY_CHANNEL_STRUCTURE
    for cat, struct in CATEGORY_CHANNEL_STRUCTURE.items():
        names.add(cat)
        names.update(struct["text"])
        names.update(struct["voice"])
    return names


def is_kamla_channel(channel) -> bool:
    if channel.name in kamla_managed_names() or ROOM_CATEGORY_RE.fullmatch(channel.name):
        return True
    category = getattr(channel, "category", None)
    return bool(category is not None and ROOM_CATEGORY_RE.fullmatch(category.name))


def check_permissions(guild: discord.Guild) -> list[str]:
    """Names of the permissions KAMLA is missing (empty = fine)."""
    perms = guild.me.guild_permissions
    if perms.administrator:
        return []
    return [p for p in REQUIRED_PERMISSIONS if not getattr(perms, p)]


def hierarchy_problems(guild: discord.Guild) -> list[str]:
    """KAMLA tournament roles that KAMLA's own role does not outrank (it cannot manage or hand those out)."""
    mine = kamla_role(guild)
    me_top = getattr(guild.me, "top_role", None)
    top = me_top if me_top is not None else mine
    if top is None:
        return ["KAMLA has no role of its own"]
    return [r.name for r in guild.roles
            if r.name in KAMLA_ROLE_NAMES and not r.managed and r.position >= top.position]


@dataclass
class Check:
    key: str
    label: str
    state: str = "pending"          # pending | ok | failed
    detail: str = ""


@dataclass
class Compatibility:
    checks: list[Check] = field(default_factory=list)
    foreign_channels: list[str] = field(default_factory=list)
    foreign_roles: list[str] = field(default_factory=list)
    already_installed: bool = False
    ok: bool = False
    headline: str = ""


def check_compatibility(guild: discord.Guild) -> Compatibility:
    """Read-only inspection: existing channels / categories / roles, permissions, role hierarchy. Never changes anything."""
    out = Compatibility()
    cfg = config_manager.get_cached(guild.id) or {}

    foreign_ch = [c for c in guild.channels if not is_kamla_channel(c) and c.name not in DEFAULT_NAMES]
    foreign_roles = [r.name for r in guild.roles
                     if not r.is_default() and not r.managed and r.name not in KAMLA_ROLE_NAMES]
    out.foreign_channels = [c.name for c in foreign_ch]
    out.foreign_roles = foreign_roles
    out.already_installed = bool(cfg.get("format")) or any(
        c.name in (CAT_ORGCOM, CAT_GA, CAT_PLAY, CAT_INFO, CAT_SECURITY) for c in guild.categories)

    structure = Check("compat", "Checking server compatibility")
    if out.already_installed:
        structure.state, structure.detail = "ok", "KAMLA structure found — setup will resume and only add what is missing"
    elif foreign_ch or foreign_roles:
        structure.state = "failed"
        bits = []
        if foreign_ch:
            bits.append(f"{len(foreign_ch)} existing channel(s)/categor(ies)")
        if foreign_roles:
            bits.append(f"{len(foreign_roles)} existing role(s)")
        structure.detail = "This server is already populated: " + " and ".join(bits)
    else:
        structure.state, structure.detail = "ok", "Fresh server"

    missing = check_permissions(guild)
    perms = Check("perms", "Checking permissions", "failed" if missing else "ok",
                  ("Missing: " + ", ".join(p.replace("_", " ").title() for p in missing)) if missing else "All required permissions granted")

    bad = hierarchy_problems(guild)
    hier = Check("hierarchy", "Checking role hierarchy", "failed" if bad else "ok",
                 ("KAMLA's role must be above: " + ", ".join(bad)) if bad else "KAMLA's role is above every role it manages")
    out.checks = [structure, perms, hier]
    out.ok = all(c.state == "ok" for c in out.checks)
    if out.ok:
        out.headline = "✅ Safe to install."
    elif structure.state == "failed":
        out.headline = ("⛔ This server already has channels or roles. **KAMLA recommends a fresh server** — it will not "
                        "wipe or restructure an existing one, so no setup is offered here.")
    else:
        out.headline = "⛔ Setup is paused until the problems below are fixed."
    return out


# ── status messages ──────────────────────────────────────────────────────────────────────────────────────────

ICON = {"ok": "✅", "failed": "❌", "pending": "⬜", "running": "⏳", "skipped": "➖"}


def compat_embed(compat: Compatibility, installer: discord.abc.User | None) -> discord.Embed:
    who = installer.mention if installer is not None else "there"
    embed = discord.Embed(
        title="👋 I'M HERE",
        description=(f"Hello {who}! I'm **KAMLA**, your tournament server manager.\n"
                     "Only you (the person who added me) and I can see this channel.\n\n" + compat.headline),
        color=discord.Color.green() if compat.ok else discord.Color.red(),
    )
    embed.add_field(name="Compatibility check",
                    value="\n".join(f"{ICON[c.state]} **{c.label}** — {c.detail}" for c in compat.checks), inline=False)
    if compat.ok:
        embed.add_field(name="Next", value="Press **Create Now** (or run `/st` here).", inline=False)
    return embed


PHASES = [
    ("compat", "Checking server compatibility"),
    ("perms", "Checking permissions"),
    ("hierarchy", "Checking role hierarchy"),
    ("roles", "Creating roles"),
    ("categories", "Creating categories"),
    ("channels", "Creating channels"),
    ("permissions", "Configuring permissions"),
    ("done", "Installation completed"),
]


def planned_totals(fmt: str, rooms: int) -> dict[str, int]:
    """Exactly how many roles / categories / channels this install is made of (used for honest x/y counters)."""
    from server_builder import ROLE_CONFIG, CATEGORY_CHANNEL_STRUCTURE, TOP_LEVEL_CHANNELS, room_channel_count
    cats = len(CATEGORY_CHANNEL_STRUCTURE) + rooms
    chans = len(TOP_LEVEL_CHANNELS) + sum(len(s["text"]) + len(s["voice"]) for s in CATEGORY_CHANNEL_STRUCTURE.values())
    chans += rooms * (room_channel_count(fmt) - 1)
    return {"roles": len(ROLE_CONFIG), "categories": cats, "channels": chans}


class InstallTracker:
    """Renders the install into ONE message in server-status. Every number comes from the real BuildReport (what has
    actually been created or found), every phase flips to ✅ only when its work finished — nothing is estimated."""

    def __init__(self, message, totals: dict[str, int], compat: Compatibility, title: str = "⚙️ Installing KAMLA"):
        self.message = message
        self.totals = totals
        self.title = title
        self.state = {k: "pending" for k, _ in PHASES}
        for c in compat.checks:
            self.state[c.key] = c.state
        self.detail: dict[str, str] = {}
        self.counts = {"roles": 0, "categories": 0, "channels": 0}
        self.failed_text = ""
        self.final: str | None = None
        self._last_edit = 0.0

    # counters derive from the report: created + reused items of each kind
    @staticmethod
    def _tally(report) -> dict[str, int]:
        items = list(report.created) + list(report.reused)
        roles = sum(1 for i in items if i.startswith("role "))
        cats = sum(1 for i in items if i.startswith("category "))
        chans = sum(1 for i in items if i.startswith(("#", "🔊", "🔈", "voice ")))
        return {"roles": roles, "categories": cats, "channels": chans}

    async def update(self, report, phase: str) -> None:
        self.counts = self._tally(report)
        t = self.totals
        if phase == "roles":
            self.state["roles"] = "running"
        elif phase == "roles_done":
            self.state["roles"] = "ok"
            self.state["categories"] = "running"
            self.state["channels"] = "running"
        elif phase == "structure":
            self.state["categories"] = "ok" if self.counts["categories"] >= t["categories"] else "running"
        elif phase == "permissions":
            self.state["categories"] = "ok" if self.counts["categories"] >= t["categories"] else "failed"
            self.state["channels"] = "ok" if self.counts["channels"] >= t["channels"] else "failed"
            self.state["permissions"] = "running"
        if report.failed:
            self.failed_text = report.failure_text(4)
        await self.render()

    async def finish(self, ok: bool, text: str) -> None:
        if ok:
            self.state["permissions"] = "ok"
        elif self.state["permissions"] != "ok":
            self.state["permissions"] = "failed"
        self.state["done"] = "ok" if ok else "failed"
        self.final = text
        await self.render(force=True)

    def embed(self) -> discord.Embed:
        done_items = sum(self.counts.values())
        total_items = sum(self.totals.values())
        lines = []
        for key, label in PHASES:
            st = self.state[key]
            extra = ""
            if key in self.counts and st in ("running", "ok", "failed") or key in self.counts and self.counts[key]:
                extra = f" — {self.counts[key]}/{self.totals[key]}"
            lines.append(f"{ICON.get(st, '⬜')} {label}{extra}")
        remaining = [label for key, label in PHASES if self.state[key] in ("pending", "running")]
        color = discord.Color.green() if self.state["done"] == "ok" else (
            discord.Color.red() if self.state["done"] == "failed" else discord.Color.yellow())
        embed = discord.Embed(title=self.title, description="\n".join(lines), color=color)
        embed.add_field(name="Created or found so far",
                        value=f"**{done_items}/{total_items}** resources ({done_items * 100 // max(total_items, 1)}%)",
                        inline=True)
        if remaining:
            embed.add_field(name="Remaining steps", value=", ".join(remaining)[:1000], inline=False)
        if self.failed_text:
            embed.add_field(name="Problems", value=self.failed_text[:1000], inline=False)
        if self.final:
            embed.add_field(name="Result", value=self.final[:1000], inline=False)
        return embed

    async def render(self, force: bool = False) -> None:
        now = _clock()
        if not force and now - self._last_edit < 2.0:
            return                                   # a status line, not a request per step
        self._last_edit = now
        try:
            await edits.run(lambda: self.message.edit(embed=self.embed(), view=None), label="install-status")
        except Exception as e:  # noqa: BLE001 - the display must never break an install
            log.debug("status edit failed: %s", describe(e))


# ── management channels ──────────────────────────────────────────────────────────────────────────────────────

def installer_of(guild: discord.Guild, cfg: dict | None = None) -> int:
    cfg = cfg if cfg is not None else (config_manager.get_cached(guild.id) or {})
    return int(cfg.get("installer_id") or guild.owner_id or 0)


def status_overwrites(guild: discord.Guild, installer: discord.abc.Snowflake | None) -> dict:
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                               embed_links=True, manage_messages=True, manage_channels=True,
                                               manage_threads=True),
    }
    if installer is not None:
        ow[installer] = discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                    send_messages=False, use_application_commands=True)
    return with_thread_lock(guild, ow)


async def ensure_status_channel(guild: discord.Guild, installer: discord.abc.Snowflake | None):
    channel = discord.utils.get(guild.text_channels, name=CH_STATUS)
    if channel is not None:
        return channel
    from server_builder import _pace
    await _pace(guild)
    return await bulk.run(
        lambda: guild.create_text_channel(CH_STATUS, overwrites=status_overwrites(guild, installer),
                                          topic="KAMLA installation status — visible only to the installer and KAMLA.",
                                          reason="KAMLA installation"),
        label="create server-status")


async def setup_on_join(guild: discord.Guild, installer: discord.abc.User | None):
    """Immediately create kamla-config and server-status and post "I'M HERE" with the compatibility check."""
    from server_builder import _pace
    await _pace(guild)
    await config_manager.ensure_config_channel(guild)
    owner = guild.get_member(guild.owner_id) if installer is None else None
    who = installer or owner
    if who is not None:
        await config_manager.update_config(guild, installer_id=who.id)
    status = await ensure_status_channel(guild, who)
    compat = check_compatibility(guild)
    view = CreateNowView() if compat.ok else None
    await bulk.run(lambda: status.send(embed=compat_embed(compat, who), view=view), label="send I'M HERE")
    return status, compat


# ── Create Now / modal ───────────────────────────────────────────────────────────────────────────────────────

class SetupModal(discord.ui.Modal, title="KAMLA — Create your server"):
    tournament = discord.ui.TextInput(label="Tournament name", max_length=90, placeholder="MY OPEN 2026")
    fmt = discord.ui.TextInput(label="Format (AP or BP)", max_length=2, default="AP")
    rooms = discord.ui.TextInput(label="Number of debate rooms (1-1000)", max_length=4, default="5")
    role = discord.ui.TextInput(label="Your role (ORG, CAP, TABBY or EQUITY)", max_length=6, default="ORG")

    async def on_submit(self, interaction: discord.Interaction):
        fmt = str(self.fmt).strip().lower()
        role = str(self.role).strip().upper()
        try:
            rooms = int(str(self.rooms).strip())
        except ValueError:
            rooms = 0
        if fmt not in ("ap", "bp") or role not in ("ORG", "CAP", "TABBY", "EQUITY") or not 1 <= rooms <= 1000:
            await interaction.response.send_message(
                "❌ Please enter format **AP** or **BP**, a room count from 1 to 1000, and a role of ORG, CAP, TABBY or EQUITY.",
                ephemeral=True)
            return
        await interaction.response.send_message("🚀 Starting the installation — follow it in this channel.", ephemeral=True)
        await run_install(interaction.guild, interaction.channel, interaction.user, {
            "format": fmt, "rooms": rooms, "timezone": "+06:00",
            "tournament_name": str(self.tournament).strip(), "role_name": role})


async def authorize_installer(interaction: discord.Interaction) -> str | None:
    """None when allowed; otherwise the message to show. Only in server-status and only for the installer."""
    if interaction.guild is None:
        return "This only works inside a server."
    if getattr(interaction.channel, "name", None) != CH_STATUS:
        return f"⛔ Use this in **#{CH_STATUS}** only."
    cfg = config_manager.get_cached(interaction.guild.id) or {}
    if interaction.user.id != installer_of(interaction.guild, cfg):
        return "⛔ Only the person who added KAMLA to this server can do this."
    return None


class CreateNowView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Now", style=discord.ButtonStyle.success, custom_id="kamla:on_join:create_now", emoji="⚡")
    async def create_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        denied = await authorize_installer(interaction)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        compat = check_compatibility(interaction.guild)          # re-check: things may have changed since the join
        if not compat.ok:
            await interaction.response.send_message(compat.headline, ephemeral=True)
            return
        await interaction.response.send_modal(SetupModal())


# ── the install itself ───────────────────────────────────────────────────────────────────────────────────────

_running: set[int] = set()


async def run_install(guild: discord.Guild, channel, user: discord.abc.User, setup_data: dict) -> None:
    """Check → (if safe) build with live, accurate progress in `channel` (server-status)."""
    from server_builder import (build_server, building_job, capacity_problem_fresh_build, attach_tracker,
                                detach_tracker, BuildInProgress, BuildIncomplete)
    from setup_cog import _assign_creator_role
    fmt, rooms = setup_data["format"], setup_data["rooms"]

    compat = check_compatibility(guild)
    if not compat.ok:
        await channel.send(embed=compat_embed(compat, user))
        return
    problem = capacity_problem_fresh_build(fmt, rooms)
    if problem:
        await channel.send(f"⛔ {problem}")
        return
    if guild.id in _running:
        await channel.send("⏳ An installation is already running.")
        return
    _running.add(guild.id)
    tracker = None
    try:
        totals = planned_totals(fmt, rooms)
        message = await channel.send(embed=discord.Embed(title="⚙️ Installing KAMLA", description="Starting…",
                                                         color=discord.Color.yellow()))
        tracker = InstallTracker(message, totals, compat)
        await tracker.render(force=True)
        attach_tracker(guild, tracker)
        try:
            async with building_job(guild):
                report = await build_server(
                    guild=guild, fmt=fmt, rooms=rooms, timezone=setup_data["timezone"],
                    tournament_name=setup_data["tournament_name"], creator_id=user.id)
                await _assign_creator_role(guild, user, setup_data["role_name"])
                from webhook import edit_join_log
                await edit_join_log(guild, await config_manager.get_config(guild))
        except BuildInProgress as e:
            await tracker.finish(False, str(e))
            return
        except BuildIncomplete as e:
            await tracker.update(e.report, "structure")
            await tracker.finish(False, "Incomplete — run `/st` again with the same settings; KAMLA resumes and "
                                        "creates only what is missing.")
            return
        except Exception as e:  # noqa: BLE001
            log.exception("Install failed in guild %s", guild.id)
            await tracker.finish(False, f"Failed: {describe(e)}")
            return
        await config_manager.update_config(guild, installed=True)
        await tracker.update(report, "permissions")
        await tracker.finish(True, "**Server Completed** 🎉\n" + report.summary())
    finally:
        detach_tracker(guild)
        _running.discard(guild.id)


# ── hierarchy reminders (server-status, at most once per 5 minutes, never a DM) ──────────────────────────────

async def remind_hierarchy(guild: discord.Guild) -> bool:
    """Warn the installer in server-status when KAMLA's role is below roles it manages. Returns True if posted."""
    problems = hierarchy_problems(guild)
    if not problems:
        _last_reminder.pop(guild.id, None)
        return False
    now = _clock()
    last = _last_reminder.get(guild.id)
    if last is not None and now - last < REMINDER_INTERVAL:
        return False
    channel = discord.utils.get(guild.text_channels, name=CH_STATUS)
    if channel is None:
        return False
    _last_reminder[guild.id] = now
    who = f"<@{installer_of(guild)}>"
    await bulk.best_effort(lambda: channel.send(
        f"⚠️ {who} KAMLA's role is **below** {', '.join(problems)}, so it cannot manage them. "
        "Open **Server Settings → Roles** and drag **KAMLA's role to the very top**. "
        "I'll pause anything that needs those roles until then.",
        allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=installer_of(guild))])), label="hierarchy-warning")
    return True

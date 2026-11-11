"""
tabbycat_cog.py — Tabbycat integration for KAMLA.

  /tab        TABBY role only. Opens a small form: Tabbycat site URL, tournament slug, API token.
              (also: /tab status, /tab disconnect)
  /refresh    CAP / ORG / TABBY. Re-reads rounds, motions, draws, results and breaks from Tabbycat and
              reports what is available.
  /draw  /motion  /result  /break
              CAP / ORG / TABBY. After the command, Discord shows a  round:  box (a dropdown, like AP/BP in /st)
              that lists ONLY the rounds whose draw / motions / results are released. The last line of the list is
              always "⚠️ ... please /refresh to get latest update!", and it is the only line when nothing is released.
              Each command can be used in ANY channel; the result is rendered as an image (no text) and posted in
              the right channel (matchup / motion / ballot / break).
  /motion     Choosing a round posts NOTHING by itself. KAMLA checks the round's motions for an info slide:
              - info slide:    [Reveal Info Slide] first, then [Reveal Motion] (unlocked once the info slide is out)
              - no info slide: a single [Reveal Motion] button
              The image appears in the motion channel only when a button is pressed.

Where the connection lives: in the server's hidden kamla-config channel (readable only by KAMLA and
administrators), never in logs and never shown back. Old copies of the config are deleted when the token changes.
"""

import asyncio
import io
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import tab_render
from config import config_manager
from ratelimit import describe, notify
from tabbycat_client import (BreakCategoryInfo, RoundInfo, TabbycatClient, TabbycatError, validate_base_url,
                             validate_slug)

log = logging.getLogger("kamla.tabbycat")


def _new_client(url: str, slug: str, token: str) -> TabbycatClient:
    """Single place clients are created (tests replace it to talk to a local server)."""
    return TabbycatClient(url, slug, token)

TAB_ROLE = "TABBY"
OPERATOR_ROLES = ("CAP", "ORG", "TABBY")
CACHE_TTL = 60.0              # commands re-read Tabbycat when their data is older than this
AUTO_TTL = 30.0               # a round dropdown being opened re-reads Tabbycat when data is older than this
AUTOCOMPLETE_WAIT = 2.3       # Discord gives autocomplete 3 s; slower refreshes continue in the background
FAIL_BACKOFF = 15.0           # after a failed auto-refresh don't hammer the site again for this long
MAX_FILES_PER_MESSAGE = 10
CHANNELS = {
    "draw": "🎓︱matchup",
    "motion": "🎓︱motion",
    "result": "🎓︱ballot",
    "break": "🎓︱break",
}
NONE_VALUE = -1


@dataclass
class Cache:
    tournament: str
    rounds: list[RoundInfo]
    breaks: list[tuple[BreakCategoryInfo, int]] = field(default_factory=list)   # (category, breaking teams)
    fetched: float = field(default_factory=time.monotonic)

    def stale(self) -> bool:
        return time.monotonic() - self.fetched > CACHE_TTL

    def available_rounds(self, kind: str) -> list[RoundInfo]:
        test = {"draw": lambda r: r.draw_available,
                "motion": lambda r: r.motions_available,
                "result": lambda r: r.results_available}[kind]
        return [r for r in self.rounds if test(r)]

    def summary(self) -> str:
        def seqs(kind: str) -> str:
            rs = self.available_rounds(kind)
            return ", ".join(r.name for r in rs) if rs else "none yet"
        brk = ", ".join(c.name for c, n in self.breaks if n) or "none yet"
        return (f"**Draw:** {seqs('draw')}\n**Motions:** {seqs('motion')}\n"
                f"**Results:** {seqs('result')}\n**Break:** {brk}")


def _member_roles(member) -> set[str]:
    return {r.name for r in getattr(member, "roles", [])}


def is_tab_admin(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return (TAB_ROLE in _member_roles(member) or bool(perms and perms.administrator)
            or getattr(member.guild, "owner_id", None) == member.id)


def is_operator(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return (bool(_member_roles(member) & set(OPERATOR_ROLES)) or bool(perms and perms.administrator)
            or getattr(member.guild, "owner_id", None) == member.id)


class TabModal(discord.ui.Modal, title="Connect Tabbycat"):
    def __init__(self, cog: "TabbycatCog", current: Optional[dict] = None) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        current = current or {}
        self.url = discord.ui.TextInput(label="Tabbycat site URL", placeholder="https://yourtournament.example.com",
                                        default=current.get("url", ""), max_length=200)
        self.slug = discord.ui.TextInput(label="Tournament slug", placeholder="e.g. kijani2026 (from the tournament URL)",
                                         default=current.get("slug", ""), max_length=100)
        self.token = discord.ui.TextInput(
            label="API token", required=False, max_length=100,
            placeholder="Leave empty to keep the saved token" if current.get("token") else "Tabbycat → Admin → Users → API token")
        for item in (self.url, self.slug, self.token):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.connect(interaction, self.url.value, self.slug.value, self.token.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Tab modal failed: %s", describe(error))
        try:
            await interaction.followup.send("❌ Something went wrong while saving. Please try again.", ephemeral=True)
        except Exception:  # noqa: BLE001
            pass


class _RevealButton(discord.ui.Button):
    def __init__(self, kind: str, idx: Optional[int], **kw) -> None:
        super().__init__(**kw)
        self.kind, self.idx = kind, idx                 # kind: "info" | "motion"; idx: 1-based motion, None = all

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.press(interaction, self)


class MotionRevealView(discord.ui.View):
    """Buttons shown by /motion (nothing is posted until one is pressed; only CAP / ORG / TABBY can press).

    One motion:   [Reveal Info Slide] (only if it has one) then [Reveal Motion] (locked until the info slide is out).
    Several:      row(s) of  [Info 1] [Info 2] ... [All info slides]   and   [Motion 1] [Motion 2] ... [All motions].
                  Each motion unlocks after its own info slide (if it has one). The "All" buttons post whatever has
                  not been revealed yet, in one go. Every button works once and greys out when used."""

    def __init__(self, cog: "TabbycatCog", rnd: RoundInfo, tournament: str, has_info: Optional[bool] = None) -> None:
        super().__init__(timeout=900)
        self.cog, self.rnd, self.tournament = cog, rnd, tournament
        self.total = len(rnd.motions)
        self.with_info = [i for i, m in enumerate(rnd.motions, 1) if m.info_slide]
        self.info_done: set[int] = set()
        self.motion_done: set[int] = set()
        self.info_buttons: dict[Optional[int], _RevealButton] = {}
        self.motion_buttons: dict[Optional[int], _RevealButton] = {}
        self._build()
        self._sync()

    # -- layout ------------------------------------------------------------------

    def _build(self) -> None:
        single = self.total <= 1
        info_specs, motion_specs = [], []
        if single:
            if self.with_info:
                info_specs.append((1, "Reveal Info Slide"))
            motion_specs.append((1, "Reveal Motion"))
        else:
            per_motion = len(self.with_info) + self.total + 2 <= 20            # always fits 5 rows; else only the "All" buttons
            if self.with_info:
                if per_motion:
                    info_specs += [(i, f"Info {i}") for i in self.with_info]
                info_specs.append((None, "All info slides"))
            if per_motion:
                motion_specs += [(i, f"Motion {i}") for i in range(1, self.total + 1)]
            motion_specs.append((None, "All motions"))
        row = 0
        for specs, kind, store, style, emoji in ((info_specs, "info", self.info_buttons, discord.ButtonStyle.primary, "📄"),
                                                 (motion_specs, "motion", self.motion_buttons, discord.ButtonStyle.success, "🎯")):
            for pos, (idx, label) in enumerate(specs):
                btn = _RevealButton(kind, idx, label=label, row=row + pos // 5,
                                    style=discord.ButtonStyle.secondary if (idx is None and len(specs) > 1) else style,
                                    emoji=emoji)
                store[idx] = btn
                self.add_item(btn)
            row += (len(specs) + 4) // 5

    @property
    def reveal_info(self) -> _RevealButton:                  # the single-motion buttons (kept for convenience)
        return next(iter(self.info_buttons.values()))

    @property
    def reveal_motion(self) -> _RevealButton:
        return next(iter(self.motion_buttons.values()))

    def intro(self) -> str:
        name = f"**{self.rnd.name}**"
        if self.total <= 1:
            return (f"{name} has an info slide. Press **Reveal Info Slide** first, then **Reveal Motion**."
                    if self.with_info else f"{name} — press **Reveal Motion** to post the motion.")
        extra = " Each motion unlocks after its info slide." if self.with_info else ""
        return (f"{name} has {self.total} motions. Reveal them one by one, or use the **All** buttons.{extra}"
                " Each button works once.")

    # -- state -------------------------------------------------------------------

    def _locked(self, idx: int) -> bool:
        return idx in self.with_info and idx not in self.info_done

    def _sync(self) -> None:
        for idx, btn in self.info_buttons.items():
            remaining = [i for i in self.with_info if i not in self.info_done]
            btn.disabled = (not remaining) if idx is None else idx in self.info_done
        for idx, btn in self.motion_buttons.items():
            remaining = [i for i in range(1, self.total + 1) if i not in self.motion_done]
            if idx is None:
                btn.disabled = not remaining or any(self._locked(i) for i in remaining)
            else:
                btn.disabled = idx in self.motion_done or self._locked(idx)

    def _targets(self, button: _RevealButton) -> list[int]:
        if button.kind == "info":
            pool = [i for i in self.with_info if i not in self.info_done]
        else:
            pool = [i for i in range(1, self.total + 1) if i not in self.motion_done and not self._locked(i)]
        return pool if button.idx is None else [i for i in pool if i == button.idx]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_operator(interaction.user):
            return True
        await interaction.response.send_message("⛔ Only CAP, ORG and TABBY can use this.", ephemeral=True)
        return False

    async def press(self, interaction: discord.Interaction, button: _RevealButton) -> None:
        targets = self._targets(button)
        done = self.info_done if button.kind == "info" else self.motion_done
        done.update(targets)                                      # greys the button(s) out at once: no double click
        self._sync()
        await interaction.response.edit_message(view=self)
        ok = bool(targets) and await self.cog._post_motion_images(
            interaction, self.rnd, self.tournament, info_only=button.kind == "info", only=targets)
        if not ok:
            done.difference_update(targets)
            self._sync()
            try:
                await interaction.edit_original_response(view=self)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log.error("Motion reveal failed: %s", describe(error))
        try:
            await interaction.followup.send("❌ Something went wrong. Please run /motion again.", ephemeral=True)
        except Exception:  # noqa: BLE001
            pass


class TabbycatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._clients: dict[int, tuple[tuple, TabbycatClient]] = {}
        self._cache: dict[int, Cache] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._warm_task: Optional[asyncio.Task] = None
        self._warmed = False
        self._auto_tasks: dict[int, asyncio.Task] = {}
        self._backoff: dict[int, float] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        tab_render.check_fonts()

    async def cog_unload(self) -> None:
        for task in [self._warm_task, *self._auto_tasks.values()]:
            if task and not task.done():
                task.cancel()
        self._auto_tasks.clear()
        for _, client in list(self._clients.values()):
            await client.close()
        self._clients.clear()

    @commands.Cog.listener()
    async def on_ready(self):
        if self._warmed:
            return
        self._warmed = True
        self._warm_task = asyncio.get_running_loop().create_task(self._warm())

    async def _warm(self) -> None:
        """After a restart, reload the round cache for connected servers so the options are ready."""
        for guild in list(self.bot.guilds):
            try:
                cfg = await config_manager.get_config(guild)
                if cfg.get("tabbycat"):
                    await self._refresh(guild)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Could not preload Tabbycat data for guild %s: %s", guild.id, describe(e))

    # ── connection plumbing ───────────────────────────────────────────────────

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def _tab_config(self, guild: discord.Guild) -> Optional[dict]:
        cfg = await config_manager.get_config(guild)
        tab = cfg.get("tabbycat")
        return tab if tab and tab.get("url") and tab.get("slug") else None

    async def _client(self, guild: discord.Guild) -> Optional[TabbycatClient]:
        tab = await self._tab_config(guild)
        if not tab:
            return None
        ident = (tab["url"], tab["slug"], tab.get("token", ""))
        existing = self._clients.get(guild.id)
        if existing and existing[0] == ident:
            return existing[1]
        if existing:
            await existing[1].close()
        client = _new_client(tab["url"], tab["slug"], tab.get("token", ""))
        self._clients[guild.id] = (ident, client)
        return client

    async def _drop_client(self, guild_id: int) -> None:
        existing = self._clients.pop(guild_id, None)
        if existing:
            await existing[1].close()
        self._cache.pop(guild_id, None)

    async def _refresh(self, guild: discord.Guild) -> Cache:
        async with self._lock(guild.id):
            client = await self._client(guild)
            if client is None:
                raise TabbycatError("config", "This server is not connected to Tabbycat yet. A TABBY must run /tab.")
            client.forget_names()
            tournament, rounds = await asyncio.gather(client.tournament(), client.rounds())
            breaks = []
            try:
                for cat in await client.break_categories():
                    teams = await client.breaking_teams(cat)
                    breaks.append((cat, len(teams)))
            except TabbycatError as exc:
                if exc.kind in ("auth", "notfound"):
                    log.info("Break data not accessible in guild %s (%s).", guild.id, exc.kind)
                else:
                    raise
            cache = Cache(tournament=tournament.get("short_name") or tournament.get("name") or "Tournament",
                          rounds=rounds, breaks=breaks)
            self._cache[guild.id] = cache
            return cache

    async def _ensure_cache(self, guild: discord.Guild) -> Cache:
        cache = self._cache.get(guild.id)
        if cache is None or cache.stale():
            try:
                return await self._refresh(guild)
            except TabbycatError:
                if cache is not None:
                    return cache          # keep working from the last good data
                raise
        return cache

    async def _autocomplete_cache(self, guild: discord.Guild) -> Optional[Cache]:
        """Data for a dropdown: re-read Tabbycat when it is older than AUTO_TTL, but never make Discord wait
        more than AUTOCOMPLETE_WAIT (a slow refresh keeps running and the next keystroke gets its result).
        One refresh at a time per server, and a short back-off after a failure. Falls back to the last good data."""
        cache = self._cache.get(guild.id)
        now = time.monotonic()
        if cache is not None and now - cache.fetched < AUTO_TTL:
            return cache
        task = self._auto_tasks.get(guild.id)
        if task is None or task.done():
            if now < self._backoff.get(guild.id, 0.0):
                return cache
            task = asyncio.get_running_loop().create_task(self._refresh(guild))
            task.add_done_callback(lambda t, gid=guild.id: self._auto_done(gid, t))
            self._auto_tasks[guild.id] = task
        try:
            return await asyncio.wait_for(asyncio.shield(task), AUTOCOMPLETE_WAIT)
        except asyncio.TimeoutError:
            return self._cache.get(guild.id)
        except Exception:  # noqa: BLE001  (already logged by _auto_done)
            return self._cache.get(guild.id)

    def _auto_done(self, guild_id: int, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()                                  # also marks it as retrieved
        if exc is not None:
            self._backoff[guild_id] = time.monotonic() + FAIL_BACKOFF
            log.info("Auto-refresh failed for guild %s: %s", guild_id, describe(exc))

    # ── /tab ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="tab", description="Connect this server to Tabbycat (TABBY only).")
    @app_commands.describe(action="connect (default), status or disconnect")
    @app_commands.choices(action=[app_commands.Choice(name="connect", value="connect"),
                                  app_commands.Choice(name="status", value="status"),
                                  app_commands.Choice(name="disconnect", value="disconnect")])
    @app_commands.guild_only()
    async def tab(self, interaction: discord.Interaction, action: Optional[app_commands.Choice[str]] = None):
        if not is_tab_admin(interaction.user):
            await interaction.response.send_message("⛔ Only members with the TABBY role can use /tab.", ephemeral=True)
            return
        chosen = action.value if action else "connect"
        guild = interaction.guild
        current = await self._tab_config(guild)
        if chosen == "connect":
            await interaction.response.send_modal(TabModal(self, current))
        elif chosen == "status":
            await self._status(interaction, current)
        else:
            await self._disconnect(interaction, current)

    async def _status(self, interaction: discord.Interaction, current: Optional[dict]) -> None:
        if not current:
            await interaction.response.send_message("Not connected. Run `/tab` to connect to Tabbycat.", ephemeral=True)
            return
        fonts = tab_render.check_fonts()
        host = current["url"].split("://", 1)[-1]
        cache = self._cache.get(interaction.guild.id)
        embed = discord.Embed(title="Tabbycat connection", color=discord.Color.blurple())
        embed.add_field(name="Site", value=host, inline=True)
        embed.add_field(name="Tournament slug", value=current["slug"], inline=True)
        embed.add_field(name="API token", value="saved ✓" if current.get("token") else "none (public data only)", inline=True)
        embed.add_field(name="Cached data",
                        value=cache.summary() if cache else "Not loaded yet — run `/refresh`.", inline=False)
        embed.add_field(name="Image fonts",
                        value=(f"E.ttf {'✓' if fonts['E.ttf'] else '✗ (fallback)'}  ·  B.ttf {'✓' if fonts['B.ttf'] else '✗ (fallback)'}"
                               f"  ·  Bengali shaping {'✓' if fonts['raqm'] else '✗ MISSING'}"
                               f"  ·  Emoji {'✓' if fonts['emoji'] else '✗ (left out)'}"), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _disconnect(self, interaction: discord.Interaction, current: Optional[dict]) -> None:
        if not current:
            await interaction.response.send_message("Nothing to disconnect.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = await config_manager.get_config(interaction.guild)
        cfg.pop("tabbycat", None)
        await config_manager.set_config(interaction.guild, cfg)
        await config_manager.compact(interaction.guild)           # remove old copies that contained the token
        await self._drop_client(interaction.guild.id)
        await interaction.followup.send("✅ Disconnected from Tabbycat. The saved token was removed.", ephemeral=True)

    async def connect(self, interaction: discord.Interaction, url: str, slug: str, token: str) -> None:
        guild = interaction.guild
        try:
            current = await self._tab_config(guild) or {}
            base = validate_base_url(url)
            slug = validate_slug(slug)
            token = (token or "").strip() or current.get("token", "")
            test = _new_client(base, slug, token)
            try:
                tournament = await test.tournament()
                await test.rounds()
            finally:
                await test.close()
        except TabbycatError as exc:
            await interaction.followup.send(f"❌ Could not connect: {exc.message}", ephemeral=True)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("Unexpected error while connecting Tabbycat")
            await interaction.followup.send(f"❌ Could not connect ({type(e).__name__}).", ephemeral=True)
            return

        cfg = await config_manager.get_config(guild)
        token_changed = token != current.get("token", "")
        cfg["tabbycat"] = {"url": base, "slug": slug, "token": token}
        await config_manager.set_config(guild, cfg)
        if token_changed:
            await config_manager.compact(guild)               # old config copies may hold the previous token
        await self._drop_client(guild.id)

        try:
            cache = await self._refresh(guild)
            body = cache.summary()
        except Exception as exc:  # noqa: BLE001
            msg = exc.message if isinstance(exc, TabbycatError) else "unexpected error"
            body = f"Saved, but the first data load failed: {msg}\nTry `/refresh` in a moment."
        name = tournament.get("name") or slug
        await interaction.followup.send(
            embed=discord.Embed(title=f"✅ Connected to {name}", description=body, color=discord.Color.green())
            .set_footer(text="CAP, ORG and TABBY can now use /draw, /motion, /result, /break and /refresh"),
            ephemeral=True)

    # ── /refresh ──────────────────────────────────────────────────────────────

    @app_commands.command(name="refresh", description="Reload rounds, draws, motions, results and breaks from Tabbycat.")
    @app_commands.guild_only()
    async def refresh(self, interaction: discord.Interaction):
        if not await self._operator_gate(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            cache = await self._refresh(interaction.guild)
        except TabbycatError as exc:
            await interaction.followup.send(f"❌ {exc.message}", ephemeral=True)
            return
        except Exception:  # noqa: BLE001
            log.exception("Unexpected error during /refresh")
            await interaction.followup.send("❌ Could not refresh from Tabbycat. Please try again.", ephemeral=True)
            return
        await interaction.followup.send(
            embed=discord.Embed(title="🔄 Updated from Tabbycat", description=cache.summary(), color=discord.Color.green()),
            ephemeral=True)

    # ── option lists (autocomplete) ───────────────────────────────────────────

    async def _round_choices(self, interaction: discord.Interaction, current: str, kind: str):
        guild = interaction.guild
        label = {"draw": "draw", "motion": "motions", "result": "results"}[kind]
        if not await self._tab_config(guild):
            return [app_commands.Choice(name="Not connected — a TABBY must run /tab", value=NONE_VALUE)]
        cache = await self._autocomplete_cache(guild)
        if cache is None:
            return [app_commands.Choice(name="⚠️ Could not load from Tabbycat — please /refresh to get latest update!",
                                        value=NONE_VALUE)]
        rounds = [r for r in cache.available_rounds(kind) if current.lower() in r.name.lower()]
        choices = [app_commands.Choice(name=r.name[:100], value=r.seq) for r in rounds[:24]]
        if not cache.available_rounds(kind):
            warning = f"⚠️ No {label} released yet — please /refresh to get latest update!"
        else:
            warning = "⚠️ Round not here? Please /refresh to get latest update!"
        choices.append(app_commands.Choice(name=warning, value=NONE_VALUE))
        return choices

    async def _draw_ac(self, i: discord.Interaction, current: str):
        return await self._round_choices(i, current, "draw")

    async def _motion_ac(self, i: discord.Interaction, current: str):
        return await self._round_choices(i, current, "motion")

    async def _result_ac(self, i: discord.Interaction, current: str):
        return await self._round_choices(i, current, "result")

    async def _break_ac(self, interaction: discord.Interaction, current: str):
        if not await self._tab_config(interaction.guild):
            return [app_commands.Choice(name="Not connected — a TABBY must run /tab", value="-")]
        cache = await self._autocomplete_cache(interaction.guild)
        if cache is None:
            return [app_commands.Choice(name="⚠️ Could not load from Tabbycat — please /refresh to get latest update!",
                                        value="-")]
        cats = [c for c, n in cache.breaks if n and current.lower() in c.name.lower()]
        if not cats:
            return [] if any(n for _, n in cache.breaks) else [
                app_commands.Choice(name="⚠️ No break released yet — please /refresh to get latest update!", value="-")]
        return [app_commands.Choice(name=c.name[:100], value=c.name[:100]) for c in cats[:25]]

    # ── shared command plumbing ───────────────────────────────────────────────

    async def _operator_gate(self, interaction: discord.Interaction) -> bool:
        if not is_operator(interaction.user):
            await interaction.response.send_message("⛔ Only CAP, ORG and TABBY can use this command.", ephemeral=True)
            return False
        return True

    async def _prepare(self, interaction: discord.Interaction):
        """Permission check + defer + fresh-enough cache. Returns (client, cache) or None (already answered)."""
        if not await self._operator_gate(interaction):
            return None
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self._client(interaction.guild)
        if client is None:
            await interaction.followup.send("This server is not connected to Tabbycat yet. A TABBY must run `/tab` first.",
                                            ephemeral=True)
            return None
        try:
            cache = await self._ensure_cache(interaction.guild)
        except TabbycatError as exc:
            await interaction.followup.send(f"❌ {exc.message}\nUse `/refresh` once the problem is fixed.", ephemeral=True)
            return None
        except Exception:  # noqa: BLE001
            log.exception("Unexpected error while loading Tabbycat data")
            await interaction.followup.send("❌ Could not load data from Tabbycat. Try `/refresh` in a moment.", ephemeral=True)
            return None
        return client, cache

    async def _post(self, interaction: discord.Interaction, kind: str, files: list[tuple[str, bytes]]) -> None:
        guild = interaction.guild
        channel = discord.utils.get(guild.text_channels, name=CHANNELS[kind]) or interaction.channel
        for start in range(0, len(files), MAX_FILES_PER_MESSAGE):
            chunk = files[start:start + MAX_FILES_PER_MESSAGE]
            await notify.run(
                lambda c=chunk: channel.send(files=[discord.File(io.BytesIO(data), filename=name) for name, data in c]),
                label=f"tab-{kind}")
        where = channel.mention if hasattr(channel, "mention") else "this channel"
        await interaction.followup.send(f"✅ Posted in {where}.", ephemeral=True)

    @staticmethod
    def _pick_round(cache: Cache, kind: str, seq: int) -> Optional[RoundInfo]:
        return next((r for r in cache.available_rounds(kind) if r.seq == seq), None)

    async def _none_available(self, interaction: discord.Interaction, kind: str, rnd: Optional[RoundInfo] = None) -> None:
        label = {"draw": "draw", "motion": "motions", "result": "results", "break": "break"}[kind]
        what = f"No {label} for {rnd.name}" if rnd is not None else f"No {label} is available for that choice"
        await interaction.followup.send(
            f"⚠️ {what} yet. Please use `/refresh` to get the latest update from Tabbycat, then try again.",
            ephemeral=True)

    async def _run(self, interaction: discord.Interaction, kind: str, seq: int, build) -> None:
        prepared = await self._prepare(interaction)
        if prepared is None:
            return
        client, cache = prepared
        rnd = self._pick_round(cache, kind, seq) if seq != NONE_VALUE else None
        if rnd is None:
            await self._none_available(interaction, kind, next((r for r in cache.rounds if r.seq == seq), None))
            return
        try:
            pages = await build(client, cache, rnd)
        except TabbycatError as exc:
            await interaction.followup.send(f"❌ {exc.message}", ephemeral=True)
            return
        except Exception:  # noqa: BLE001
            log.exception("Rendering /%s failed", kind)
            await interaction.followup.send("❌ Could not build the image. Please try again.", ephemeral=True)
            return
        if not pages:
            await self._none_available(interaction, kind)
            return
        slug = rnd.name.lower().replace(" ", "-")
        await self._post(interaction, kind, [(f"{kind}-{slug}-{i}.png", png) for i, png in enumerate(pages, 1)])

    # ── /draw  /motion  /result  /break ───────────────────────────────────────

    @app_commands.command(name="draw", description="Post a round's draw as an image.")
    @app_commands.describe(round_="Round (only rounds with a released draw are listed)")
    @app_commands.rename(round_="round")
    @app_commands.autocomplete(round_=_draw_ac)
    @app_commands.guild_only()
    async def draw(self, interaction: discord.Interaction, round_: int):
        async def build(client, cache, rnd):
            debates = await client.pairings(rnd)
            if not debates:
                return []
            return await asyncio.to_thread(tab_render.render_draw, cache.tournament, rnd, debates)
        await self._run(interaction, "draw", round_, build)

    @app_commands.command(name="motion", description="Reveal a round's info slide and motion as images.")
    @app_commands.describe(round_="Round (only rounds with released motions are listed)")
    @app_commands.rename(round_="round")
    @app_commands.autocomplete(round_=_motion_ac)
    @app_commands.guild_only()
    async def motion(self, interaction: discord.Interaction, round_: int):
        prepared = await self._prepare(interaction)
        if prepared is None:
            return
        _, cache = prepared
        rnd = self._pick_round(cache, "motion", round_) if round_ != NONE_VALUE else None
        if rnd is None:
            await self._none_available(interaction, "motion", next((r for r in cache.rounds if r.seq == round_), None))
            return
        view = MotionRevealView(self, rnd, cache.tournament)
        await interaction.followup.send(view.intro(), view=view, ephemeral=True)

    async def _post_motion_images(self, interaction: discord.Interaction, rnd: RoundInfo, tournament: str,
                                  *, info_only: bool, only: Optional[list[int]] = None) -> bool:
        """Render + post the motions (or only the info slides) of a round; `only` = 1-based positions.
        Returns True when something was posted."""
        try:
            if info_only:
                pages = await asyncio.to_thread(tab_render.render_info_slides, tournament, rnd, rnd.motions, only)
                prefix = "info"
                numbers = [i for i, m in enumerate(rnd.motions, 1) if m.info_slide and (only is None or i in only)]
            else:
                pages = await asyncio.to_thread(tab_render.render_motions, tournament, rnd, rnd.motions, False, only)
                prefix = "motion"
                numbers = [i for i in range(1, len(rnd.motions) + 1) if only is None or i in only]
        except Exception:  # noqa: BLE001
            log.exception("Rendering /motion failed")
            await interaction.followup.send("❌ Could not build the image. Please try again.", ephemeral=True)
            return False
        if not pages:
            await self._none_available(interaction, "motion", rnd)
            return False
        slug = rnd.name.lower().replace(" ", "-")
        await self._post(interaction, "motion", [(f"{prefix}-{slug}-{n}.png", png) for n, png in zip(numbers, pages)])
        return True

    @app_commands.command(name="result", description="Post a round's results as an image.")
    @app_commands.describe(round_="Round (only rounds with results are listed)")
    @app_commands.rename(round_="round")
    @app_commands.autocomplete(round_=_result_ac)
    @app_commands.guild_only()
    async def result(self, interaction: discord.Interaction, round_: int):
        async def build(client, cache, rnd):
            results = await client.results(rnd)
            if not results:
                return []
            return await asyncio.to_thread(tab_render.render_results, cache.tournament, rnd, results)
        await self._run(interaction, "result", round_, build)

    @app_commands.command(name="break", description="Post a break as an image.")
    @app_commands.describe(category="Break category (only categories with a break are listed)")
    @app_commands.autocomplete(category=_break_ac)
    @app_commands.guild_only()
    async def break_(self, interaction: discord.Interaction, category: str):
        prepared = await self._prepare(interaction)
        if prepared is None:
            return
        client, cache = prepared
        entry = next(((c, n) for c, n in cache.breaks if n and c.name == category), None)
        if entry is None:
            await self._none_available(interaction, "break")
            return
        try:
            teams = await client.breaking_teams(entry[0])
            if not teams:
                await self._none_available(interaction, "break")
                return
            pages = await asyncio.to_thread(tab_render.render_break, cache.tournament, entry[0], teams)
        except TabbycatError as exc:
            await interaction.followup.send(f"❌ {exc.message}", ephemeral=True)
            return
        except Exception:  # noqa: BLE001
            log.exception("Rendering /break failed")
            await interaction.followup.send("❌ Could not build the image. Please try again.", ephemeral=True)
            return
        await self._post(interaction, "break",
                         [(f"break-{category.lower().replace(' ', '-')}-{i}.png", png) for i, png in enumerate(pages, 1)])


async def setup(bot: commands.Bot):
    await bot.add_cog(TabbycatCog(bot))

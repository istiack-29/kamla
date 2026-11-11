"""
tabbycat_cog.py — Tabbycat integration for KAMLA.

  /tab        TABBY role only. Opens a small form: Tabbycat site URL, tournament slug, API token.
              (also: /tab status, /tab disconnect)
  /refresh    CAP / ORG / TABBY. Re-reads rounds, motions, draws, results and breaks from Tabbycat and
              reports what is available.
  /draw  /motion  /result  /break
              CAP / ORG / TABBY. Choose from the options that are actually available (rounds whose draw /
              motions / results are released, break categories that have a break). Each command can be used
              in ANY channel; the result is rendered as an image (no text) and posted in the right channel
              (matchup / motion / ballot / break). If nothing is available the command says so and points to /refresh.

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
CACHE_TTL = 600.0
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


class TabbycatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._clients: dict[int, tuple[tuple, TabbycatClient]] = {}
        self._cache: dict[int, Cache] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._warm_task: Optional[asyncio.Task] = None
        self._warmed = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        tab_render.check_fonts()

    async def cog_unload(self) -> None:
        if self._warm_task and not self._warm_task.done():
            self._warm_task.cancel()
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
                               f"  ·  Bengali shaping {'✓' if fonts['raqm'] else '✗ MISSING'}"), inline=False)
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
        cache = self._cache.get(guild.id)
        if cache is None:
            return [app_commands.Choice(name="Data not loaded — use /refresh", value=NONE_VALUE)]
        rounds = [r for r in cache.available_rounds(kind) if current.lower() in r.name.lower()]
        if not rounds:
            if cache.available_rounds(kind):
                return []
            return [app_commands.Choice(name=f"No {label} released yet — use /refresh", value=NONE_VALUE)]
        return [app_commands.Choice(name=r.name[:100], value=r.seq) for r in rounds[:25]]

    async def _draw_ac(self, i: discord.Interaction, current: str):
        return await self._round_choices(i, current, "draw")

    async def _motion_ac(self, i: discord.Interaction, current: str):
        return await self._round_choices(i, current, "motion")

    async def _result_ac(self, i: discord.Interaction, current: str):
        return await self._round_choices(i, current, "result")

    async def _break_ac(self, interaction: discord.Interaction, current: str):
        if not await self._tab_config(interaction.guild):
            return [app_commands.Choice(name="Not connected — a TABBY must run /tab", value="-")]
        cache = self._cache.get(interaction.guild.id)
        if cache is None:
            return [app_commands.Choice(name="Data not loaded — use /refresh", value="-")]
        cats = [c for c, n in cache.breaks if n and current.lower() in c.name.lower()]
        if not cats:
            return [] if any(n for _, n in cache.breaks) else [
                app_commands.Choice(name="No break released yet — use /refresh", value="-")]
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
        sent = 0
        for start in range(0, len(files), MAX_FILES_PER_MESSAGE):
            chunk = files[start:start + MAX_FILES_PER_MESSAGE]
            await notify.run(
                lambda c=chunk: channel.send(files=[discord.File(io.BytesIO(data), filename=name) for name, data in c]),
                label=f"tab-{kind}")
            sent += len(chunk)
        where = channel.mention if hasattr(channel, "mention") else "this channel"
        await interaction.followup.send(f"✅ Posted in {where}.", ephemeral=True)

    @staticmethod
    def _pick_round(cache: Cache, kind: str, seq: int) -> Optional[RoundInfo]:
        return next((r for r in cache.available_rounds(kind) if r.seq == seq), None)

    async def _none_available(self, interaction: discord.Interaction, kind: str) -> None:
        label = {"draw": "draw", "motion": "motions", "result": "results", "break": "break"}[kind]
        await interaction.followup.send(
            f"No {label} is available for that choice. Use `/refresh` to update from Tabbycat, then try again.",
            ephemeral=True)

    async def _run(self, interaction: discord.Interaction, kind: str, seq: int, build) -> None:
        prepared = await self._prepare(interaction)
        if prepared is None:
            return
        client, cache = prepared
        rnd = self._pick_round(cache, kind, seq) if seq != NONE_VALUE else None
        if rnd is None:
            await self._none_available(interaction, kind)
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

    @app_commands.command(name="motion", description="Post a round's motion as an image.")
    @app_commands.describe(round_="Round (only rounds with released motions are listed)")
    @app_commands.rename(round_="round")
    @app_commands.autocomplete(round_=_motion_ac)
    @app_commands.guild_only()
    async def motion(self, interaction: discord.Interaction, round_: int):
        async def build(client, cache, rnd):
            return await asyncio.to_thread(tab_render.render_motions, cache.tournament, rnd, rnd.motions)
        await self._run(interaction, "motion", round_, build)

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

"""
KAMLABot — setup_nuke.py
Cog: SetupNuke

/startb workflow:
    1. Identity Gate (DM) — executor picks ORG / CAP / Tabby / Equity Officer.
    2. Tournament config wizard (DM) — type, format, rooms, server name.
    3. Cyberpunk terminal embed in DM (live ASCII bar + ANSI scroll log).
    4. Nuke channels + roles, lockdown @everyone.
    5. Create roles, assign executor's selected role.
    6. BaseBuilder.build_all() → RoomEngine.build_rooms().
    7. Setup Silence Protocol active throughout (logger ignores events).
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands


# (key, display, color, admin, moderator)
ROLE_DEFINITIONS = [
    ("ORG",             "ORG",                 0xE74C3C, True,  False),
    ("Tabby",           "Tabby",               0x9B59B6, True,  False),
    ("CAP",             "CAP",                 0x3498DB, False, True),
    ("Equity Officer",  "Equity Officer",      0x2ECC71, False, True),
    ("Invited Adj",     "Invited Adj 🟢",      0x27AE60, False, False),
    ("Independent Adj", "Independent Adj 🔴",  0xE74C3C, False, False),
    ("Debater",         "Debater ⚫",          0x2C3E50, False, False),
    ("Visitor",         "Visitor ⚪",          0x95A5A6, False, False),
]

IDENTITY_CHOICES = ["ORG", "CAP", "Tabby", "Equity Officer"]

RATE_SLEEP = 1.2
BAR_LEN = 24

ANSI_RESET = "\u001b[0m"
ANSI_CYAN = "\u001b[1;36m"
ANSI_GREEN = "\u001b[1;32m"
ANSI_YELLOW = "\u001b[1;33m"
ANSI_RED = "\u001b[1;31m"
ANSI_GREY = "\u001b[1;30m"


class TerminalUI:
    """Live cyberpunk terminal embed in DM. Updates every ~2s."""

    def __init__(self, dm: discord.DMChannel, title: str = "KAMLABot — Boot Sequence"):
        self.dm = dm
        self.title = title
        self.message: discord.Message | None = None
        self.pct = 0
        self.lines: list[str] = []
        self._lock = asyncio.Lock()
        self._dirty = True
        self._task: asyncio.Task | None = None

    def _render(self) -> discord.Embed:
        filled = int(BAR_LEN * (self.pct / 100))
        bar = "█" * filled + "░" * (BAR_LEN - filled)
        color = ANSI_GREEN if self.pct >= 100 else ANSI_CYAN
        tail = self.lines[-12:] if self.lines else [f"{ANSI_GREY}[idle]{ANSI_RESET}"]
        body = (
            "```ansi\n"
            f"{color}╔══════════ KAMLABot ══════════╗{ANSI_RESET}\n"
            f"{color}║ [{bar}] {self.pct:3d}% ║{ANSI_RESET}\n"
            f"{color}╚══════════════════════════════╝{ANSI_RESET}\n"
            + "\n".join(tail)
            + "\n```"
        )
        e = discord.Embed(title=f"🖥️  {self.title}", description=body, color=0x00E5FF)
        e.set_footer(text="Live boot telemetry • do not close this DM")
        return e

    async def start(self):
        self.message = await self.dm.send(embed=self._render())
        self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        try:
            while True:
                await asyncio.sleep(2.0)
                if self._dirty and self.message:
                    self._dirty = False
                    try:
                        await self.message.edit(embed=self._render())
                    except discord.HTTPException:
                        pass
        except asyncio.CancelledError:
            pass

    async def log(self, line: str, level: str = "info"):
        colors = {
            "info": ANSI_CYAN,
            "ok": ANSI_GREEN,
            "warn": ANSI_YELLOW,
            "err": ANSI_RED,
            "purge": ANSI_RED,
            "build": ANSI_GREEN,
        }
        c = colors.get(level, ANSI_CYAN)
        self.lines.append(f"{c}{line}{ANSI_RESET}")
        self._dirty = True

    async def set_progress(self, pct: int):
        self.pct = max(0, min(100, pct))
        self._dirty = True

    async def finish(self, ok: bool = True):
        self.pct = 100
        await self.log("[DONE] All systems online." if ok else "[FAIL] Boot aborted.",
                       "ok" if ok else "err")
        if self._task:
            self._task.cancel()
        if self.message:
            try:
                await self.message.edit(embed=self._render())
            except discord.HTTPException:
                pass


class SetupNuke(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── on_ready: lock @everyone on first join ───────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            try:
                await self._lockdown_everyone(guild)
            except discord.HTTPException:
                pass

    async def _lockdown_everyone(self, guild: discord.Guild):
        perms = guild.default_role.permissions
        perms.update(
            send_messages=False, add_reactions=False, create_public_threads=False,
            create_private_threads=False, speak=False, connect=False,
        )
        try:
            await guild.default_role.edit(permissions=perms, reason="KAMLABot lockdown")
        except discord.HTTPException:
            pass

    # ── helpers ──────────────────────────────────────────────────────────────
    async def _delete_all_channels(self, guild, term: TerminalUI):
        for ch in list(guild.channels):
            try:
                await asyncio.sleep(RATE_SLEEP)
                await ch.delete(reason="KAMLABot /startb nuke")
                await term.log(f"[PURGE] #{ch.name}", "purge")
            except discord.HTTPException:
                pass

    async def _delete_all_roles(self, guild, term: TerminalUI):
        bot_top = guild.me.top_role
        for role in list(guild.roles):
            if role.is_default() or role.managed or role >= bot_top:
                continue
            try:
                await asyncio.sleep(RATE_SLEEP)
                await role.delete(reason="KAMLABot /startb nuke")
                await term.log(f"[PURGE] @{role.name}", "purge")
            except discord.HTTPException:
                pass

    async def _create_roles(self, guild, term: TerminalUI) -> dict[str, discord.Role]:
        created = {}
        for key, name, color, is_admin, is_mod in ROLE_DEFINITIONS:
            await asyncio.sleep(RATE_SLEEP)
            perms = discord.Permissions.none()
            if is_admin:
                perms = discord.Permissions.all()
            elif is_mod:
                perms.update(
                    manage_messages=True, kick_members=True, mute_members=True,
                    move_members=True, manage_roles=False,
                )
            try:
                role = await guild.create_role(
                    name=name, color=discord.Color(color),
                    permissions=perms, hoist=True, mentionable=True,
                    reason="KAMLABot /startb",
                )
                created[key] = role
                self.bot.state["roles"][key] = role.id
                await term.log(f"[BUILD] role @{name}", "build")
            except discord.HTTPException as e:
                await term.log(f"[ERR] role {name}: {e}", "err")
        return created

    # ── DM wizard helpers ────────────────────────────────────────────────────
    async def _ask(self, user: discord.User, dm: discord.DMChannel, prompt: str,
                   validator) -> str | None:
        await dm.send(prompt)
        def check(m: discord.Message):
            return m.author.id == user.id and isinstance(m.channel, discord.DMChannel)
        for _ in range(5):
            try:
                msg = await self.bot.wait_for("message", timeout=120, check=check)
            except asyncio.TimeoutError:
                await dm.send("⌛ Timed out. Run `/startb` again.")
                return None
            ok, val = validator(msg.content.strip())
            if ok:
                return val
            await dm.send("❌ Invalid. Try again.")
        await dm.send("❌ Too many invalid attempts.")
        return None

    # ── /startb ──────────────────────────────────────────────────────────────
    @app_commands.command(name="startb", description="Nuke + build the tournament server.")
    async def startb(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You need Administrator to run this.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ Run inside a guild.", ephemeral=True)
            return

        try:
            dm = await interaction.user.create_dm()
        except discord.HTTPException:
            await interaction.response.send_message("❌ Can't DM you.", ephemeral=True)
            return

        await interaction.response.send_message("📬 Setup wizard sent to your DMs.", ephemeral=True)

        # ── 1) Identity Gate ────────────────────────────────────────────────
        identity_prompt = (
            "**🆔 Identity Gate**\nSelect your role:\n"
            "`1` ORG\n`2` CAP\n`3` Tabby\n`4` Equity Officer"
        )
        def v_identity(s):
            return (s in {"1", "2", "3", "4"}, int(s) - 1 if s in {"1", "2", "3", "4"} else None)
        idx = await self._ask(interaction.user, dm, identity_prompt, v_identity)
        if idx is None:
            return
        executor_role_key = IDENTITY_CHOICES[idx]

        # ── 2) Tournament wizard ────────────────────────────────────────────
        def v_choice(opts):
            def inner(s):
                return (s in opts, int(s) if s in opts else None)
            return inner
        def v_int(lo, hi):
            def inner(s):
                if s.isdigit() and lo <= int(s) <= hi:
                    return True, int(s)
                return False, None
            return inner
        def v_str(s):
            return (1 <= len(s) <= 60, s)

        ttype = await self._ask(interaction.user, dm,
            "**🏆 Tournament Type**\n`1` Regular\n`2` Fundraiser", v_choice({"1", "2"}))
        if ttype is None: return
        fmt = await self._ask(interaction.user, dm,
            "**🎙️ Format**\n`1` Asian Parli (AP)\n`2` British Parli (BP)", v_choice({"1", "2"}))
        if fmt is None: return
        rooms = await self._ask(interaction.user, dm,
            "**🏟️ Number of rooms** (1-20)", v_int(1, 20))
        if rooms is None: return
        sname = await self._ask(interaction.user, dm,
            "**🪧 Server name** (1-60 chars)", v_str)
        if sname is None: return

        state = self.bot.state
        state["tournament_type"] = ttype
        state["format"] = fmt
        state["num_rooms"] = rooms
        state["server_name"] = sname

        # ── 3) Terminal UI ──────────────────────────────────────────────────
        term = TerminalUI(dm, title=f"Boot Sequence — {sname}")
        await term.start()

        # ── Silence audit logger for the duration ───────────────────────────
        state["is_setup_active"] = True

        try:
            await term.log("[INIT] Engaging nuke protocol...", "warn")
            await term.set_progress(5)

            await self._delete_all_channels(guild, term)
            await term.set_progress(25)
            await self._delete_all_roles(guild, term)
            await term.set_progress(40)

            await term.log("[LOCK] @everyone lockdown", "info")
            await self._lockdown_everyone(guild)

            await term.log("[BUILD] Creating role matrix...", "build")
            roles = await self._create_roles(guild, term)
            await term.set_progress(55)

            # rename guild
            try:
                await asyncio.sleep(RATE_SLEEP)
                await guild.edit(name=sname, reason="KAMLABot /startb")
            except discord.HTTPException:
                pass

            # assign executor's identity role
            exec_role = roles.get(executor_role_key)
            if exec_role:
                try:
                    member = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)
                    await member.add_roles(exec_role, reason="KAMLABot identity gate")
                    await term.log(f"[GRANT] @{exec_role.name} → {member}", "ok")
                except discord.HTTPException:
                    pass

            # base build
            base = self.bot.get_cog("BaseBuilder")
            if base:
                await term.log("[BUILD] Base infrastructure...", "build")
                await base.build_all(guild, roles, term=term)
            await term.set_progress(80)

            # rooms
            room_engine = self.bot.get_cog("RoomEngine")
            if room_engine:
                await term.log(f"[BUILD] {rooms} debate rooms...", "build")
                await room_engine.build_rooms(guild, roles, fmt, rooms)
            await term.set_progress(100)

            await term.finish(ok=True)
        except Exception as e:
            await term.log(f"[FATAL] {e}", "err")
            await term.finish(ok=False)
        finally:
            state["is_setup_active"] = False


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupNuke(bot))

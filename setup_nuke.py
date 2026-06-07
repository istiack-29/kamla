"""
KAMLABot — setup_nuke.py
Cog: SetupNuke

Handles:
  • /startb  — Nuke protocol + role creation + DM setup wizard
  • @everyone server-wide lockdown on bot join

Identity Gate:
  Instead of auto-assigning ORG, the wizard prompts the administrator with
  a numbered choice: 1=ORG, 2=CAP, 3=Tabby, 4=Equity Officer.
  The matching role is fetched and assigned at build finalization.

Live Cyberpunk Terminal Logs:
  A single DM embed is edited precisely every 2.0 seconds during the build phase.
  It displays a block-styled progress bar and an ANSI terminal codeblock feeding
  live execution lines.

Setup Silence Protocol:
  bot.state["is_setup_active"] is set to True at the start of the build phase
  and False upon absolute completion. The AuditLogger cog reads this flag to
  silence event spam during mass initialization.
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone


ROLE_DEFINITIONS = [
    ("ORG",               "ORG",                    0xE74C3C, True,  False),
    ("Tabby",             "Tabby",                  0x9B59B6, True,  False),
    ("CAP",               "CAP",                    0x3498DB, False, True ),
    ("Equity Officer",    "Equity Officer",          0x2ECC71, False, True ),
    ("Invited Adj",       "Invited Adj 🟢",          0x27AE60, False, False),
    ("Independent Adj",   "Independent Adj 🔴",      0xE74C3C, False, False),
    ("Debater",           "Debater ⚫",              0x2C3E50, False, False),
    ("Visitor",           "Visitor ⚪",              0x95A5A6, False, False),
]

IDENTITY_CHOICES = {
    "1": "ORG",
    "2": "CAP",
    "3": "Tabby",
    "4": "Equity Officer",
}

BAR_TOTAL = 16  # Block progress bar character width


def _build_progress_bar(current: int, total: int) -> str:
    """Render a block-styled progress bar: [████████░░░░░░░░] 50%"""
    if total == 0:
        return "[" + "░" * BAR_TOTAL + "] 0%"
    filled = min(int((current / total) * BAR_TOTAL), BAR_TOTAL)
    empty = BAR_TOTAL - filled
    pct = int((current / total) * 100)
    return f"[{'█' * filled}{'░' * empty}] {pct}%"


def _build_live_embed(
    server_name: str,
    progress_bar: str,
    log_lines: list[str],
) -> discord.Embed:
    """
    Build the live cyberpunk terminal DM embed.
    Top: block progress bar.
    Bottom: ANSI codeblock terminal feed.
    """
    ansi_lines = "\n".join(log_lines[-12:]) if log_lines else "Initializing…"

    embed = discord.Embed(
        title=f"⚡ KAMLABot — Building: {server_name}",
        color=discord.Color.dark_red(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="Progress",
        value=f"`{progress_bar}`",
        inline=False,
    )
    embed.add_field(
        name="Terminal",
        value=f"```ansi\n{ansi_lines}\n```",
        inline=False,
    )
    embed.set_footer(text="KAMLABot Setup Protocol — DO NOT close your DMs")
    return embed


class LiveProgressTracker:
    """
    Manages the live DM terminal embed that is edited every 2.0 seconds.
    Other cogs call .push_log() to feed execution lines.
    The background task handles periodic editing independently.
    """

    def __init__(
        self,
        dm_channel: discord.DMChannel,
        server_name: str,
        total_steps: int,
    ):
        self.dm = dm_channel
        self.server_name = server_name
        self.total_steps = total_steps
        self.current_step = 0
        self.log_lines: list[str] = []
        self._message: discord.Message | None = None
        self._task: asyncio.Task | None = None
        self._dirty = False
        self._stopped = False

    async def start(self):
        """Post the initial embed and start the background edit loop."""
        embed = _build_live_embed(
            self.server_name,
            _build_progress_bar(0, self.total_steps),
            ["\u001b[1;33mSYSTEM\u001b[0m] Initializing KAMLABot build protocol…"],
        )
        try:
            self._message = await self.dm.send(embed=embed)
        except discord.HTTPException:
            pass
        self._task = asyncio.create_task(self._edit_loop())

    async def _edit_loop(self):
        """Edit the embed every 2.0 seconds while active."""
        while not self._stopped:
            await asyncio.sleep(2.0)
            if self._dirty and self._message:
                bar = _build_progress_bar(self.current_step, self.total_steps)
                embed = _build_live_embed(self.server_name, bar, self.log_lines)
                try:
                    await self._message.edit(embed=embed)
                    self._dirty = False
                except (discord.NotFound, discord.HTTPException):
                    pass

    def push_log(self, line: str, step_increment: int = 1):
        """Append a terminal line and advance the progress counter."""
        self.log_lines.append(line)
        self.current_step = min(self.current_step + step_increment, self.total_steps)
        self._dirty = True

    async def finalize(self, success: bool = True):
        """Stop the loop and do a final edit showing completion."""
        self._stopped = True
        if self._task:
            self._task.cancel()

        if not self._message:
            return

        bar = _build_progress_bar(self.total_steps, self.total_steps)
        status_line = (
            "\u001b[1;32mSYSTEM\u001b[0m] ✅ Build complete — server is ready!"
            if success
            else "\u001b[1;31mSYSTEM\u001b[0m] ❌ Build aborted."
        )
        self.log_lines.append(status_line)
        embed = _build_live_embed(self.server_name, bar, self.log_lines)
        try:
            await self._message.edit(embed=embed)
        except (discord.NotFound, discord.HTTPException):
            pass


class SetupNuke(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not hasattr(self.bot, "state"):
            self.bot.state = {}
            
        # Shared tracker accessible to BaseBuilder and RoomEngine via bot.state
        self.bot.state["_progress_tracker"] = None

    # ── Helper: rate-limited deletion ─────────────────────────────────────────
    async def _delete_all_channels(self, guild: discord.Guild):
        tracker: LiveProgressTracker | None = self.bot.state.get("_progress_tracker")
        for channel in list(guild.channels):
            try:
                await channel.delete(reason="KAMLABot /startb nuke")
                await asyncio.sleep(1.2)
                if tracker:
                    tracker.push_log(
                        f"\u001b[1;31mPURGE\u001b[0m] Wiping #{channel.name}… SUCCESS"
                    )
            except discord.HTTPException as e:
                if tracker:
                    tracker.push_log(
                        f"\u001b[1;31mPURGE\u001b[0m] Wiping #{channel.name}… SKIP ({e.status})"
                    )

    async def _delete_all_roles(self, guild: discord.Guild):
        tracker: LiveProgressTracker | None = self.bot.state.get("_progress_tracker")
        bot_top_role = guild.me.top_role
        for role in list(guild.roles):
            if role.is_default() or role.managed or role >= bot_top_role:
                continue
            try:
                await role.delete(reason="KAMLABot /startb nuke")
                await asyncio.sleep(1.2)
                if tracker:
                    tracker.push_log(
                        f"\u001b[1;31mPURGE\u001b[0m] Removing @{role.name}… SUCCESS"
                    )
            except discord.HTTPException:
                pass

    async def _lockdown_everyone(self, guild: discord.Guild):
        try:
            await guild.default_role.edit(
                permissions=discord.Permissions(
                    view_channel=False,
                    send_messages=False,
                    connect=False,
                ),
                reason="KAMLABot server-wide lockdown",
            )
        except discord.HTTPException:
            pass

    async def _create_roles(self, guild: discord.Guild) -> dict:
        tracker: LiveProgressTracker | None = self.bot.state.get("_progress_tracker")
        created = {}
        for key, display, color, is_admin, is_mod in ROLE_DEFINITIONS:
            if is_admin:
                perms = discord.Permissions.all()
            elif is_mod:
                perms = discord.Permissions(
                    kick_members=True,
                    ban_members=True,
                    manage_messages=True,
                    manage_channels=True,
                    view_channel=True,
                    send_messages=True,
                    connect=True,
                    speak=True,
                    read_message_history=True,
                )
            else:
                perms = discord.Permissions(
                    view_channel=True,
                    send_messages=True,
                    connect=True,
                    speak=True,
                    read_message_history=True,
                )

            role = await guild.create_role(
                name=display,
                permissions=perms,
                color=discord.Color(color),
                hoist=True,
                reason="KAMLABot /startb role creation",
            )
            created[key] = role
            await asyncio.sleep(1.2)
            if tracker:
                tracker.push_log(
                    f"\u001b[1;32mBUILD\u001b[0m] Created role @{display}… SUCCESS"
                )

        return created

    # ── Setup wizard (DM-based) ───────────────────────────────────────────────
    async def _run_setup_wizard(
        self,
        guild: discord.Guild,
        executor: discord.Member,
    ) -> dict | None:
        try:
            dm = await executor.create_dm()
        except discord.Forbidden:
            return None

        def check(m):
            return m.author == executor and m.channel == dm

        async def ask(prompt: str, timeout: float = 120.0) -> discord.Message | None:
            await dm.send(prompt)
            try:
                return await self.bot.wait_for("message", check=check, timeout=timeout)
            except asyncio.TimeoutError:
                await dm.send("⏰ Setup timed out. Run `/startb` again to restart.")
                return None

        await dm.send(
            f"👋 **KAMLABot Setup Wizard** for **{guild.name}**\n"
            "Answer each question. Type `skip` where applicable.\n"
            "─────────────────────────────────────────────"
        )

        # Step 1: Server icon
        await dm.send(
            "**Step 1/6 — Server Icon**\n"
            "Upload an image attachment for the new server icon, or type `skip`."
        )
        try:
            icon_msg = await self.bot.wait_for("message", check=check, timeout=120.0)
        except asyncio.TimeoutError:
            await dm.send("⏰ Timed out.")
            return None

        icon_bytes = None
        if icon_msg.attachments:
            icon_bytes = await icon_msg.attachments[0].read()

        # Step 2: Server name
        name_msg = await ask(
            "**Step 2/6 — Server Name**\nWhat should this tournament server be named?"
        )
        if not name_msg:
            return None
        server_name = name_msg.content.strip()

        # Step 3: Tournament type
        type_msg = await ask(
            "**Step 3/6 — Tournament Type**\nReply with:\n`1` = Regular\n`2` = Fundraiser"
        )
        if not type_msg:
            return None
        tournament_type = int(type_msg.content.strip()) if type_msg.content.strip() in ("1", "2") else 1

        # Step 4: Format
        fmt_msg = await ask(
            "**Step 4/6 — Debate Format**\nReply with:\n`1` = AP (Asian Parliamentary)\n`2` = BP (British Parliamentary)"
        )
        if not fmt_msg:
            return None
        debate_format = int(fmt_msg.content.strip()) if fmt_msg.content.strip() in ("1", "2") else 1

        # Step 5: Number of rooms
        rooms_msg = await ask(
            "**Step 5/6 — Number of Rooms**\nEnter an integer (e.g. 20 or 40)."
        )
        if not rooms_msg:
            return None
        try:
            num_rooms = int(rooms_msg.content.strip())
        except ValueError:
            await dm.send("❌ Invalid number. Defaulting to 10.")
            num_rooms = 10

        # Step 6: Identity Gate — administrator role selection
        identity_msg = await ask(
            "**Step 6/6 — Your Role Identity**\n"
            "Which role should be assigned to YOU (the setup administrator)?\n\n"
            "`1` = ORG\n"
            "`2` = CAP\n"
            "`3` = Tabby\n"
            "`4` = Equity Officer"
        )
        if not identity_msg:
            return None
        identity_choice = identity_msg.content.strip()
        executor_role_key = IDENTITY_CHOICES.get(identity_choice, "ORG")

        await dm.send(
            f"✅ **Setup Complete!**\n"
            f"Server Name: **{server_name}**\n"
            f"Type: **{'Fundraiser' if tournament_type == 2 else 'Regular'}**\n"
            f"Format: **{'BP' if debate_format == 2 else 'AP'}**\n"
            f"Rooms: **{num_rooms}**\n"
            f"Your Role: **{executor_role_key}**\n\n"
            "Building your server now… 🏗️"
        )

        return {
            "dm_channel": dm,
            "icon_bytes": icon_bytes,
            "server_name": server_name,
            "tournament_type": tournament_type,
            "debate_format": debate_format,
            "num_rooms": num_rooms,
            "executor_role_key": executor_role_key,
        }

    # ── /startb ───────────────────────────────────────────────────────────────
    @app_commands.command(
        name="startb",
        description="Nuke the server and initialise KAMLABot tournament setup.",
    )
    @app_commands.default_permissions(administrator=True)
    async def startb(self, interaction: discord.Interaction):
        guild = interaction.guild
        executor = interaction.user

        await interaction.response.send_message(
            "🔴 **Nuke Protocol Initiated.** Check your DMs for the setup wizard.",
            ephemeral=True,
        )

        # ── 1. Lockdown @everyone ─────────────────────────────────────────────
        await self._lockdown_everyone(guild)

        # ── 2. Run the DM wizard BEFORE destruction ───────────────────────────
        settings = await self._run_setup_wizard(guild, executor)
        if not settings:
            return

        dm: discord.DMChannel = settings["dm_channel"]

        # ── 3. Estimate total build steps and activate silence + tracker ──────
        num_rooms = settings["num_rooms"]
        # Rough estimate: deletions (channels + roles) + role creation + base channels + rooms
        estimated_steps = 30 + (len(ROLE_DEFINITIONS)) + 40 + (num_rooms * 8)

        # CRITICAL FIX: Ensure bot state structures exist fully before starting
        if not hasattr(self.bot, "state"):
            self.bot.state = {}
        self.bot.state.setdefault("channels", {})
        self.bot.state.setdefault("ga_sessions", {})
        self.bot.state.setdefault("roles", {})
        self.bot.state.setdefault("room_sessions", {})
        self.bot.state.setdefault("room_channel_map", {})

        tracker = LiveProgressTracker(dm, settings["server_name"], estimated_steps)
        self.bot.state["_progress_tracker"] = tracker
        self.bot.state["is_setup_active"] = True

        await tracker.start()

        tracker.push_log("\u001b[1;33mSYSTEM\u001b[0m] Engaging nuke protocol…")

        # ── 4. Nuke channels ──────────────────────────────────────────────────
        tracker.push_log("\u001b[1;31mPURGE\u001b[0m] Wiping all channels…")
        await self._delete_all_channels(guild)

        # ── 5. Nuke roles ─────────────────────────────────────────────────────
        tracker.push_log("\u001b[1;31mPURGE\u001b[0m] Wiping all roles…")
        await self._delete_all_roles(guild)

        # ── 6. Create new roles ───────────────────────────────────────────────
        tracker.push_log("\u001b[1;32mBUILD\u001b[0m] Creating role hierarchy…")
        roles = await self._create_roles(guild)
        self.bot.state["roles"] = {k: v.id for k, v in roles.items()}

        # ── 7. Identity Gate: assign chosen role to executor ──────────────────
        executor_role_key = settings["executor_role_key"]
        executor_role = roles.get(executor_role_key)
        if executor_role:
            try:
                member = guild.get_member(executor.id) or await guild.fetch_member(executor.id)
                await member.add_roles(
                    executor_role,
                    reason=f"KAMLABot /startb — identity gate assigned {executor_role_key}"
                )
                tracker.push_log(
                    f"\u001b[1;32mASSIGN\u001b[0m] Assigning @{executor_role_key} to executor… SUCCESS"
                )
            except Exception as e:
                tracker.push_log(
                    f"\u001b[1;31mASSIGN\u001b[0m] Failed to assign role to executor: {e}"
                )
                print(f"[SetupNuke] Could not assign {executor_role_key} to executor: {e}")

        # ── 8. Update guild name & icon ───────────────────────────────────────
        edit_kwargs = {"name": settings["server_name"]}
        if settings["icon_bytes"]:
            edit_kwargs["icon"] = settings["icon_bytes"]
        try:
            await guild.edit(**edit_kwargs, reason="KAMLABot setup")
            tracker.push_log("\u001b[1;32mBUILD\u001b[0m] Guild name & icon updated… SUCCESS")
        except discord.HTTPException as e:
            tracker.push_log(f"\u001b[1;33mWARN\u001b[0m] Guild edit partial failure: {e}")
            print(f"[SetupNuke] Guild edit failed: {e}")

        # ── 9. Update bot nickname ────────────────────────────────────────────
        try:
            bot_member = guild.get_member(self.bot.user.id)
            if bot_member:
                await bot_member.edit(
                    nick=settings["server_name"][:32],
                    reason="KAMLABot nickname sync",
                )
                tracker.push_log("\u001b[1;32mBUILD\u001b[0m] Bot nickname synced… SUCCESS")
        except discord.HTTPException as e:
            print(f"[SetupNuke] Nickname update failed: {e}")

        # ── 10. Persist state ─────────────────────────────────────────────────
        self.bot.state["server_name"] = settings["server_name"]
        self.bot.state["tournament_type"] = settings["tournament_type"]
        self.bot.state["format"] = settings["debate_format"]
        self.bot.state["num_rooms"] = settings["num_rooms"]

        # ── 11. Trigger base infrastructure ──────────────────────────────────
        base_cog = self.bot.get_cog("BaseBuilder")
        room_cog = self.bot.get_cog("RoomEngine")

        if base_cog:
            tracker.push_log("\u001b[1;32mBUILD\u001b[0m] Constructing base infrastructure…")
            await base_cog.build_base(guild, roles)
            tracker.push_log("\u001b[1;32mBUILD\u001b[0m] Base infrastructure complete… SUCCESS")

        if room_cog:
            tracker.push_log(
                f"\u001b[1;32mBUILD\u001b[0m] Provisioning {num_rooms} debate room(s)…"
            )
            await room_cog.build_rooms(
                guild, roles, settings["debate_format"], settings["num_rooms"]
            )
            tracker.push_log(
                f"\u001b[1;32mBUILD\u001b[0m] All {num_rooms} room(s) provisioned… SUCCESS"
            )

        # ── 12. Lift silence protocol ─────────────────────────────────────────
        self.bot.state["is_setup_active"] = False
        self.bot.state["_progress_tracker"] = None

        # ── 13. Finalize live tracker and notify executor ─────────────────────
        await tracker.finalize(success=True)

        try:
            await dm.send(
                "🎉 **Server is ready!** All categories, channels, and rooms have been built.\n"
                f"Your role: **{executor_role_key}** has been assigned."
            )
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupNuke(bot))
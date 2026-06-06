"""
KAMLABot — setup_nuke.py
Cog: SetupNuke

Handles:
  • /startb  — Nuke protocol + role creation + DM setup wizard
  • @everyone server-wide lockdown on bot join
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone


# Roles to create with their settings.
# (name, display_name, color_hex, is_administrator, is_moderator)
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


class SetupNuke(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Helper: rate-limited deletion ────────────────────────────────────────
    async def _delete_all_channels(self, guild: discord.Guild):
        """
        Delete every channel and category in the guild.
        Rate-limit evasion: await asyncio.sleep(1.5) between each deletion
        to avoid HTTP 429 bursts.
        """
        for channel in list(guild.channels):
            try:
                await channel.delete(reason="KAMLABot /startb nuke")
                await asyncio.sleep(1.5)  # Rate-limit evasion
            except discord.HTTPException:
                pass

    async def _delete_all_roles(self, guild: discord.Guild):
        """
        Delete every deletable role.
        Skips: @everyone, managed (bot/integration) roles, roles higher than bot's top role.
        Rate-limit evasion: await asyncio.sleep(1.5) between each deletion.
        """
        bot_top_role = guild.me.top_role
        for role in list(guild.roles):
            if role.is_default() or role.managed or role >= bot_top_role:
                continue
            try:
                await role.delete(reason="KAMLABot /startb nuke")
                await asyncio.sleep(1.5)  # Rate-limit evasion
            except discord.HTTPException:
                pass

    async def _lockdown_everyone(self, guild: discord.Guild):
        """Set @everyone to view_channel=False, send_messages=False server-wide."""
        overwrite = discord.PermissionOverwrite(
            view_channel=False,
            send_messages=False,
        )
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
        """
        Create all KAMLABot roles in hierarchical order.
        Returns a dict: {internal_key: discord.Role}
        Rate-limit evasion: sleep 1.5 s between creations.
        """
        created = {}
        for key, display, color, is_admin, is_mod in ROLE_DEFINITIONS:
            perms = discord.Permissions.none()
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
            await asyncio.sleep(1.5)  # Rate-limit evasion

        return created

    # ── Setup wizard (DM-based) ───────────────────────────────────────────────
    async def _run_setup_wizard(
        self,
        guild: discord.Guild,
        executor: discord.Member,
    ) -> dict | None:
        """
        Multi-step setup wizard sent via DM.
        Returns a dict with the collected settings or None on timeout/failure.
        """
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
            "**Step 1/5 — Server Icon**\n"
            "Upload an image attachment for the new server icon, or type `skip`."
        )
        try:
            icon_msg = await self.bot.wait_for("message", check=check, timeout=120.0)
        except asyncio.TimeoutError:
            await dm.send("⏰ Timed out."); return None

        icon_bytes = None
        if icon_msg.attachments:
            icon_bytes = await icon_msg.attachments[0].read()
        # else: skip

        # Step 2: Server name
        name_msg = await ask(
            "**Step 2/5 — Server Name**\nWhat should this tournament server be named?"
        )
        if not name_msg:
            return None
        server_name = name_msg.content.strip()

        # Step 3: Tournament type
        type_msg = await ask(
            "**Step 3/5 — Tournament Type**\nReply with:\n`1` = Regular\n`2` = Fundraiser"
        )
        if not type_msg:
            return None
        tournament_type = 1 if type_msg.content.strip() == "2" else 1
        tournament_type = int(type_msg.content.strip()) if type_msg.content.strip() in ("1", "2") else 1

        # Step 4: Format
        fmt_msg = await ask(
            "**Step 4/5 — Debate Format**\nReply with:\n`1` = AP (Asian Parliamentary)\n`2` = BP (British Parliamentary)"
        )
        if not fmt_msg:
            return None
        debate_format = int(fmt_msg.content.strip()) if fmt_msg.content.strip() in ("1", "2") else 1

        # Step 5: Number of rooms
        rooms_msg = await ask(
            "**Step 5/5 — Number of Rooms**\nEnter an integer (e.g. 20 or 40)."
        )
        if not rooms_msg:
            return None
        try:
            num_rooms = int(rooms_msg.content.strip())
        except ValueError:
            await dm.send("❌ Invalid number. Defaulting to 10.")
            num_rooms = 10

        await dm.send(
            f"✅ **Setup Complete!**\n"
            f"Server Name: **{server_name}**\n"
            f"Type: **{'Fundraiser' if tournament_type == 2 else 'Regular'}**\n"
            f"Format: **{'BP' if debate_format == 2 else 'AP'}**\n"
            f"Rooms: **{num_rooms}**\n\n"
            "Building your server now… 🏗️"
        )

        return {
            "icon_bytes": icon_bytes,
            "server_name": server_name,
            "tournament_type": tournament_type,
            "debate_format": debate_format,
            "num_rooms": num_rooms,
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
            return  # Wizard timed out; abort

        # ── 3. Nuke channels ──────────────────────────────────────────────────
        await self._delete_all_channels(guild)

        # ── 4. Nuke roles ─────────────────────────────────────────────────────
        await self._delete_all_roles(guild)

        # ── 5. Create new roles ───────────────────────────────────────────────
        roles = await self._create_roles(guild)
        self.bot.state["roles"] = {k: v.id for k, v in roles.items()}

        # ── 6. Assign ORG to executor ─────────────────────────────────────────
        try:
            member = guild.get_member(executor.id) or await guild.fetch_member(executor.id)
            await member.add_roles(roles["ORG"], reason="KAMLABot /startb — ORG auto-assign")
        except Exception as e:
            print(f"[SetupNuke] Could not assign ORG to executor: {e}")

        # ── 7. Update guild name & icon ───────────────────────────────────────
        edit_kwargs = {"name": settings["server_name"]}
        if settings["icon_bytes"]:
            edit_kwargs["icon"] = settings["icon_bytes"]
        try:
            await guild.edit(**edit_kwargs, reason="KAMLABot setup")
        except discord.HTTPException as e:
            print(f"[SetupNuke] Guild edit failed: {e}")

        # ── 8. Update bot nickname to match server name ───────────────────────
        try:
            bot_member = guild.get_member(self.bot.user.id)
            if bot_member:
                await bot_member.edit(
                    nick=settings["server_name"][:32],
                    reason="KAMLABot nickname sync",
                )
        except discord.HTTPException as e:
            print(f"[SetupNuke] Nickname update failed: {e}")

        # ── 9. Persist state ──────────────────────────────────────────────────
        self.bot.state["server_name"] = settings["server_name"]
        self.bot.state["tournament_type"] = settings["tournament_type"]
        self.bot.state["format"] = settings["debate_format"]
        self.bot.state["num_rooms"] = settings["num_rooms"]

        # ── 10. Trigger base infrastructure and room generation ───────────────
        base_cog = self.bot.get_cog("BaseBuilder")
        room_cog = self.bot.get_cog("RoomEngine")

        if base_cog:
            await base_cog.build_base(guild, roles)
        if room_cog:
            await room_cog.build_rooms(guild, roles, settings["debate_format"], settings["num_rooms"])

        # Notify executor via DM
        try:
            dm = await executor.create_dm()
            await dm.send("🎉 **Server is ready!** All categories, channels, and rooms have been built.")
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupNuke(bot))

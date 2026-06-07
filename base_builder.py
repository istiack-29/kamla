"""
KAMLABot — base_builder.py
Cog: BaseBuilder

Handles creation of:
  • 👋︱meet-the-developer  (position=0, before all categories, URL buttons)
  • Global Audit Log channel (#server-audit-logs)
  • General Assets (welcome, get-role, assign, how-to-use)
  • Fundraiser Assets (if tournament_type == 2)
  • Information Hub category
  • Grand Auditorium category  (with #ga-logs button UI)
  • OrgCom Control Center category  (with 🖥️︱settings dashboard)

Ghost Auto-Assign Sync Engine:
  Reaction-role events log to #assign with format:
    🤖 [Auto Sync] /ass to:@user as:@role
  and refresh the matching see-* embed.
"""

import asyncio
from datetime import datetime, timezone
import discord
from discord.ext import commands


# ── Reaction-role mappings ────────────────────────────────────────────────────
REACTION_ROLE_MAP = {
    "🟢": "Invited Adj",
    "🔴": "Independent Adj",
    "⚫": "Debater",
    "⚪": "Visitor",
}

HIGH_COMMAND_KEYS = ["ORG", "CAP", "Tabby", "Equity Officer"]


def _get_role(guild: discord.Guild, state: dict, key: str) -> discord.Role | None:
    role_id = state["roles"].get(key)
    return guild.get_role(role_id) if role_id else None


def _allow(*roles: discord.Role | None) -> list[tuple]:
    overwrite = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
    )
    return [(r, overwrite) for r in roles if r]


def _read_only(*roles: discord.Role | None) -> list[tuple]:
    overwrite = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=False,
        read_message_history=True,
    )
    return [(r, overwrite) for r in roles if r]


def _deny(role: discord.Role | None) -> tuple | None:
    if not role:
        return None
    return (role, discord.PermissionOverwrite(view_channel=False))


# ── "See Who" ephemeral view for GA and room logs ─────────────────────────────
class SeeWhoView(discord.ui.View):
    """Persistent button attached to GA/room session embeds."""

    def __init__(self, bot: commands.Bot, category_id: int | None = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.category_id = category_id

    @discord.ui.button(label="👥 See Who", style=discord.ButtonStyle.secondary, custom_id="see_who")
    async def see_who(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.category_id is None:
            sessions = self.bot.state.get("ga_sessions", {})
        else:
            sessions = self.bot.state.get("room_sessions", {}).get(self.category_id, {})

        if not sessions:
            await interaction.response.send_message(
                "No session data recorded yet.", ephemeral=True
            )
            return

        lines = []
        for uid, data in sessions.items():
            join_str = data["join"].strftime("%H:%M:%S") if data.get("join") else "?"
            exit_str = data["exit"].strftime("%H:%M:%S") if data.get("exit") else "still in"
            if data.get("join") and data.get("exit"):
                duration = data["exit"] - data["join"]
                dur_str = str(duration).split(".")[0]
            elif data.get("join"):
                dur_str = "ongoing"
            else:
                dur_str = "?"
            lines.append(f"<@{uid}> — joined {join_str} | left {exit_str} | {dur_str}")

        embed = discord.Embed(
            title="Session Participants",
            description="\n".join(lines) or "None",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Settings Dashboard View ───────────────────────────────────────────────────
class SettingsDashboardView(discord.ui.View):
    """
    Persistent interactive dashboard posted in 🖥️︱settings.
    Four permanent buttons: Add Room, Remove Room, Switch Format, Enable Fundraiser Mode.
    """

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot
        self._fundraiser_enabled = False

    @discord.ui.button(
        label="➕ Add Room",
        style=discord.ButtonStyle.success,
        custom_id="settings_add_room",
    )
    async def add_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._has_dashboard_access(interaction):
            await interaction.response.send_message(
                "❌ Access denied. Only ORG, CAP, or Tabby may use this panel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        room_cog = self.bot.get_cog("RoomEngine")
        if not room_cog:
            await interaction.followup.send("❌ RoomEngine cog not loaded.", ephemeral=True)
            return

        success = await room_cog.spawn_room(interaction.guild)
        if success:
            state = self.bot.state
            room_map = state.get("room_channel_map", {})
            new_num = max((v.get("room_num", 0) for v in room_map.values()), default=0)
            await interaction.followup.send(
                f"✅ **ROOM {new_num}** has been successfully provisioned.", ephemeral=True
            )
        else:
            await interaction.followup.send("❌ Failed to spawn room. Check logs.", ephemeral=True)

    @discord.ui.button(
        label="➖ Remove Room",
        style=discord.ButtonStyle.danger,
        custom_id="settings_remove_room",
    )
    async def remove_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._has_dashboard_access(interaction):
            await interaction.response.send_message(
                "❌ Access denied. Only ORG, CAP, or Tabby may use this panel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        room_cog = self.bot.get_cog("RoomEngine")
        if not room_cog:
            await interaction.followup.send("❌ RoomEngine cog not loaded.", ephemeral=True)
            return

        state = self.bot.state
        room_map = state.get("room_channel_map", {})
        if not room_map:
            await interaction.followup.send("❌ No active rooms to remove.", ephemeral=True)
            return

        highest_num = max((v.get("room_num", 0) for v in room_map.values()), default=0)
        success = await room_cog.remove_room(interaction.guild)
        if success:
            await interaction.followup.send(
                f"✅ **ROOM {highest_num}** has been successfully removed.", ephemeral=True
            )
        else:
            await interaction.followup.send("❌ Failed to remove room. Check logs.", ephemeral=True)

    @discord.ui.button(
        label="🔄 Switch Format (AP/BP)",
        style=discord.ButtonStyle.primary,
        custom_id="settings_switch_format",
    )
    async def switch_format(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._has_dashboard_access(interaction):
            await interaction.response.send_message(
                "❌ Access denied. Only ORG, CAP, or Tabby may use this panel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        room_cog = self.bot.get_cog("RoomEngine")
        if not room_cog:
            await interaction.followup.send("❌ RoomEngine cog not loaded.", ephemeral=True)
            return

        state = self.bot.state
        old_format = state.get("format", 1)
        old_name = "AP" if old_format == 1 else "BP"
        new_name = "BP" if old_format == 1 else "AP"

        success = await room_cog.switch_format(interaction.guild)
        if success:
            await interaction.followup.send(
                f"✅ All rooms converted from **{old_name}** → **{new_name}** format successfully.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("❌ Format switch failed. Check logs.", ephemeral=True)

    @discord.ui.button(
        label="💸 Enable Fundraiser Mode",
        style=discord.ButtonStyle.secondary,
        custom_id="settings_fundraiser_mode",
    )
    async def fundraiser_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._has_dashboard_access(interaction):
            await interaction.response.send_message(
                "❌ Access denied. Only ORG, CAP, or Tabby may use this panel.",
                ephemeral=True,
            )
            return

        if self._fundraiser_enabled:
            await interaction.response.send_message(
                "⚠️ Fundraiser Mode is already active and cannot be toggled off.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        state = self.bot.state

        everyone = guild.default_role
        trans_ow = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False)}

        try:
            trans_cat = await guild.create_category(
                "Transparency of Transaction", overwrites=trans_ow
            )
            await asyncio.sleep(1.2)
            for ch_name in [
                "how-we-keep-transparency",
                "donation-transaction-data",
                "spend-in-event",
                "donate",
            ]:
                await guild.create_text_channel(
                    ch_name, category=trans_cat, overwrites=trans_ow
                )
                await asyncio.sleep(1.2)

            self._fundraiser_enabled = True
            button.disabled = True
            button.label = "💸 Fundraiser Mode (ACTIVE)"
            await interaction.message.edit(view=self)

            await interaction.followup.send(
                "✅ **Fundraiser Mode** activated. Transparency channels have been created.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"❌ Failed to create fundraiser channels: {e}", ephemeral=True
            )

    def _has_dashboard_access(self, interaction: discord.Interaction) -> bool:
        state = self.bot.state
        restricted_role_keys = ["ORG", "CAP", "Tabby"]
        user_role_ids = {r.id for r in interaction.user.roles}
        for key in restricted_role_keys:
            role_id = state["roles"].get(key)
            if role_id and role_id in user_role_ids:
                return True
        return False


class BaseBuilder(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Rate-limited channel creation helper ──────────────────────────────────
    async def _make_channel(
        self,
        guild: discord.Guild,
        name: str,
        category: discord.CategoryChannel | None,
        overwrites: dict,
        channel_type: str = "text",
        **kwargs,
    ) -> discord.abc.GuildChannel:
        await asyncio.sleep(1.2)
        if channel_type == "voice":
            return await guild.create_voice_channel(
                name, category=category, overwrites=overwrites, **kwargs
            )
        return await guild.create_text_channel(
            name, category=category, overwrites=overwrites, **kwargs
        )

    async def _make_category(
        self,
        guild: discord.Guild,
        name: str,
        overwrites: dict,
    ) -> discord.CategoryChannel:
        await asyncio.sleep(1.2)
        return await guild.create_category(name, overwrites=overwrites)

    # ── Build entry point ─────────────────────────────────────────────────────
    async def build_base(self, guild: discord.Guild, roles: dict):
        """Called by SetupNuke after roles are created."""
        state = self.bot.state

        everyone = guild.default_role
        org = roles.get("ORG")
        cap = roles.get("CAP")
        tabby = roles.get("Tabby")
        equity = roles.get("Equity Officer")
        invited_adj = roles.get("Invited Adj")
        indep_adj = roles.get("Independent Adj")
        debater = roles.get("Debater")
        visitor = roles.get("Visitor")

        high_command = [r for r in [org, cap, tabby, equity] if r]
        staff = [r for r in [org, cap, tabby, equity, invited_adj, indep_adj, debater] if r]

        # ── 👋︱meet-the-developer (position=0, no category, URL buttons) ──────
        dev_ow = {
            everyone: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
            )
        }
        dev_ch = await self._make_channel(guild, "👋︱meet-the-developer", None, dev_ow)
        try:
            await dev_ch.edit(position=0, reason="KAMLABot: meet-the-developer at top")
        except discord.HTTPException:
            pass

        dev_view = discord.ui.View(timeout=None)
        dev_view.add_item(discord.ui.Button(
            label="Instagram",
            url="https://instagram.com/anonymous.istiack",
            style=discord.ButtonStyle.link,
        ))
        dev_view.add_item(discord.ui.Button(
            label="Donate",
            url="https://istiack.pages.dev/#donate",
            style=discord.ButtonStyle.link,
        ))

        try:
            dev_file = discord.File("m.png")
            await dev_ch.send(file=dev_file, view=dev_view)
        except FileNotFoundError:
            await dev_ch.send(
                "👋 **Meet the Developer**\nConnect with the creator below:",
                view=dev_view,
            )

        state["channels"]["meet_dev"] = dev_ch.id

        # ── Audit log channel (no category, high-command only) ────────────────
        audit_overwrites = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in high_command:
            audit_overwrites[r] = discord.PermissionOverwrite(
                view_channel=True, send_messages=False, read_message_history=True
            )
        audit_ch = await self._make_channel(guild, "server-audit-logs", None, audit_overwrites)
        state["channels"]["audit_logs"] = audit_ch.id

        # ── General Assets ────────────────────────────────────────────────────
        general_ow = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False)}

        welcome_ch = await self._make_channel(guild, "👐🏻︱welcome", None, general_ow)
        state["channels"]["welcome"] = welcome_ch.id

        get_role_ch = await self._make_channel(guild, "🙉︱get-role", None, general_ow)
        state["channels"]["get_role"] = get_role_ch.id

        reaction_embed = discord.Embed(
            title="🎭 Get Your Role",
            description=(
                "React with the emoji that matches your role in this tournament:\n\n"
                "🟢 — Invited Adjudicator\n"
                "🔴 — Independent Adjudicator\n"
                "⚫ — Debater\n"
                "⚪ — Visitor (read-only)\n\n"
                "**ORG / CAP / Equity Officer / Tabby:** Do NOT react here — "
                "wait for manual `/ass` assignment."
            ),
            color=discord.Color.gold(),
        )
        rr_msg = await get_role_ch.send(embed=reaction_embed)
        state["channels"]["get_role_msg"] = rr_msg.id
        for emoji in REACTION_ROLE_MAP:
            await rr_msg.add_reaction(emoji)
            await asyncio.sleep(0.5)

        assign_ow = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in [org, cap, tabby]:
            if r:
                assign_ow[r] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        assign_ch = await self._make_channel(guild, "😭︱assign", None, assign_ow)
        state["channels"]["assign"] = assign_ch.id

        howto_ch = await self._make_channel(guild, "❓︱how-to-use-this-server", None, general_ow)
        state["channels"]["how_to_use"] = howto_ch.id
        await self._post_how_to_use(howto_ch)

        # ── Fundraiser Assets (if selected during wizard) ─────────────────────
        if state.get("tournament_type") == 2:
            trans_ow = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False)}
            trans_cat = await self._make_category(guild, "Transparency of Transaction", trans_ow)
            for ch_name in [
                "how-we-keep-transparency",
                "donation-transaction-data",
                "spend-in-event",
                "donate",
            ]:
                await self._make_channel(guild, ch_name, trans_cat, trans_ow)

        # ── Information Hub ───────────────────────────────────────────────────
        info_ow = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False)}
        info_cat = await self._make_category(guild, "🤷🏻‍♂️︱INFORMATION", info_ow)

        send_ow = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False)}
        for r in [org, cap]:
            if r:
                send_ow[r] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        for ch_name in ["schedule", "important-forms", "debater-briffing", "equity-briffing", "judge-briffing"]:
            await self._make_channel(guild, ch_name, info_cat, send_ow)

        see_channels = {
            "see-org":                      "ORG",
            "see-cap":                      "CAP",
            "see-invited-adjudicators":     "Invited Adj",
            "see-independent-adjudicators": "Independent Adj",
            "see-debaters":                 "Debater",
        }
        for ch_name, role_key in see_channels.items():
            ch = await self._make_channel(guild, ch_name, info_cat, info_ow)
            state["channels"][ch_name.replace("-", "_")] = ch.id
            embed = discord.Embed(
                title=f"Members — {ch_name.replace('see-', '').replace('-', ' ').title()}",
                description="_No members assigned yet._",
                color=discord.Color.blue(),
            )
            msg = await ch.send(embed=embed)
            state["channels"][f"{ch_name.replace('-', '_')}_msg"] = msg.id

        # ── Grand Auditorium ──────────────────────────────────────────────────
        ga_ow = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False)}
        ga_cat = await self._make_category(guild, "🏟️︱Grand Auditorium", ga_ow)

        ga_chat_ow = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
        if visitor:
            ga_chat_ow[visitor] = discord.PermissionOverwrite(
                view_channel=True, send_messages=False
            )
        await self._make_channel(guild, "ga-chat", ga_cat, ga_chat_ow)

        ga_send_ow = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False)}
        for r in [org, cap, tabby]:
            if r:
                ga_send_ow[r] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        for ch_name in ["motion", "announcement", "break-announcement", "matchup", "ballot"]:
            ch = await self._make_channel(guild, ch_name, ga_cat, ga_send_ow)
            state["channels"][ch_name.replace("-", "_")] = ch.id

        ga_voice_ow = {
            everyone: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True),
        }
        if visitor:
            ga_voice_ow[visitor] = discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=False,
                use_soundboard=False,
            )
        ga_vc = await self._make_channel(
            guild, "Grand Auditorium", ga_cat, ga_voice_ow, channel_type="voice"
        )
        state["channels"]["ga_voice"] = ga_vc.id

        ga_logs_ow = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in [org, tabby]:
            if r:
                ga_logs_ow[r] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        ga_logs_ch = await self._make_channel(guild, "ga-logs", ga_cat, ga_logs_ow)
        state["channels"]["ga_logs"] = ga_logs_ch.id

        session_embed = discord.Embed(
            title="🎙️ Grand Auditorium — Session Log",
            description="No active session yet. This embed updates when the VC becomes active.",
            color=discord.Color.og_blurple(),
        )
        view = SeeWhoView(self.bot, category_id=None)
        session_msg = await ga_logs_ch.send(embed=session_embed, view=view)
        state["channels"]["ga_logs_msg"] = session_msg.id

        # ── OrgCom Control Center ─────────────────────────────────────────────
        orgcom_ow = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in high_command:
            orgcom_ow[r] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                connect=True, speak=True,
            )
        orgcom_cat = await self._make_category(guild, "⚜️︱orgcom", orgcom_ow)
        await self._make_channel(guild, "➡️︱general", orgcom_cat, orgcom_ow)
        await self._make_channel(guild, "📂︱documents", orgcom_cat, orgcom_ow)
        await self._make_channel(guild, "👨🏻‍💻︱org", orgcom_cat, orgcom_ow, channel_type="voice")
        await self._make_channel(guild, "👨🏻‍💻︱control-room", orgcom_cat, orgcom_ow, channel_type="voice")

        # ── 🖥️︱settings Dashboard Cockpit ────────────────────────────────────
        settings_ow = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in [org, cap, tabby]:
            if r:
                settings_ow[r] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=False, read_message_history=True
                )
        settings_ch = await self._make_channel(guild, "🖥️︱settings", orgcom_cat, settings_ow)
        state["channels"]["settings"] = settings_ch.id

        await self._post_settings_dashboard(settings_ch)

        # ── Initial Role Sync (Syncs Admin/Executor role right after setup) ───
        for role_key in ["ORG", "CAP", "Invited Adj", "Independent Adj", "Debater"]:
            role = roles.get(role_key)
            if role:
                await self.update_see_channel(guild, role)

    # ── Settings Dashboard Post ───────────────────────────────────────────────
    async def _post_settings_dashboard(self, channel: discord.TextChannel):
        embed = discord.Embed(
            title="🖥️ KAMLABot — Server Management Dashboard",
            description=(
                "Welcome to the **OrgCom Command Cockpit**.\n"
                "Use the controls below to manage server infrastructure in real-time.\n\n"
                "─────────────────────────────────────────\n"
                "**➕ Add Room** — Dynamically provision a new numbered debate room.\n"
                "**➖ Remove Room** — Destroy the highest-numbered active room.\n"
                "**🔄 Switch Format** — Hot-swap all rooms between AP ⇄ BP format.\n"
                "**💸 Enable Fundraiser Mode** — Inject Transparency channels *(irreversible)*.\n"
                "─────────────────────────────────────────"
            ),
            color=discord.Color.dark_gold(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="KAMLABot — Restricted Access | ORG · CAP · Tabby only")

        dashboard_view = SettingsDashboardView(self.bot)
        await channel.send(embed=embed, view=dashboard_view)

    # ── How-to-use content ────────────────────────────────────────────────────
    async def _post_how_to_use(self, channel: discord.TextChannel):
        embed = discord.Embed(
            title="❓ How to Use This Server",
            color=discord.Color.teal(),
        )
        embed.add_field(
            name="📌 Getting Your Role",
            value=(
                "Go to `🙉︱get-role` and react with the emoji that matches you:\n"
                "🟢 Invited Adj · 🔴 Independent Adj · ⚫ Debater · ⚪ Visitor\n"
                "**Staff roles** are assigned manually by the organisers."
            ),
            inline=False,
        )
        embed.add_field(
            name="🗂️ Slash Commands",
            value=(
                "`/startb` — (Admin) Initialise/reset the server\n"
                "`/ass @user as:@role` — Assign a role (use in #assign)\n"
                "`/rate` — Add reactions to the latest motion (use in #motion)\n"
                "`/allin [time]` — Move everyone into debate VC (use in room's #poi)"
            ),
            inline=False,
        )
        embed.add_field(
            name="⏱️ Visual Timer",
            value=(
                "In any room's `#timer` channel, send a message like:\n"
                "`.7m` — 7-minute timer\n"
                "`.7m30s` — 7 minutes 30 seconds\n"
                "The bot will post an ANSI-styled live countdown that updates every 5 seconds."
            ),
            inline=False,
        )
        embed.add_field(
            name="🏟️ Grand Auditorium",
            value=(
                "Join the **Grand Auditorium** voice channel for plenary sessions.\n"
                "Visitors are in listen-only mode. #ga-logs tracks session activity."
            ),
            inline=False,
        )
        embed.set_footer(text="KAMLABot — Tournament Management System")
        await channel.send(embed=embed)

    # ── Reaction-role listener (with Ghost Auto-Assign Sync Engine) ───────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        state = self.bot.state
        if payload.message_id != state["channels"].get("get_role_msg"):
            return
        if payload.user_id == self.bot.user.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        if not member:
            return

        emoji = payload.emoji.name
        role_display = REACTION_ROLE_MAP.get(emoji)
        if not role_display:
            return

        role = discord.utils.find(lambda r: r.name.startswith(role_display), guild.roles)
        if role:
            await member.add_roles(role, reason="KAMLABot reaction role")

            # Delay to allow Discord's internal members cache to reflect new role assignment
            await asyncio.sleep(0.5)

            # ── Ghost Auto-Assign Sync: log to #assign ────────────────────────
            await self._auto_sync_log(guild, member, role)

            # ── Refresh see-* embed ───────────────────────────────────────────
            await self.update_see_channel(guild, role)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        state = self.bot.state
        if payload.message_id != state["channels"].get("get_role_msg"):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        if not member:
            return

        emoji = payload.emoji.name
        role_display = REACTION_ROLE_MAP.get(emoji)
        if not role_display:
            return

        role = discord.utils.find(lambda r: r.name.startswith(role_display), guild.roles)
        if role and role in member.roles:
            await member.remove_roles(role, reason="KAMLABot reaction role removal")

            # Delay to allow Discord's internal members cache to reflect role removal
            await asyncio.sleep(0.5)

            # ── Ghost Auto-Assign Sync: log to #assign ────────────────────────
            await self._auto_sync_log(guild, member, role, removed=True)

            # ── Refresh see-* embed ───────────────────────────────────────────
            await self.update_see_channel(guild, role)

    async def _auto_sync_log(
        self,
        guild: discord.Guild,
        member: discord.Member,
        role: discord.Role,
        removed: bool = False,
    ):
        """
        Post a Ghost Auto-Assign Sync tracking log to #assign.
        Format: 🤖 [Auto Sync] /ass to:@user as:@role
        """
        state = self.bot.state
        assign_ch_id = state["channels"].get("assign")
        if not assign_ch_id:
            return
        assign_ch = guild.get_channel(assign_ch_id)
        if not assign_ch:
            return

        action = "REMOVE" if removed else "ASSIGN"
        try:
            await assign_ch.send(
                f"🤖 [Auto Sync] `/ass` **{action}** to:{member.mention} as:{role.mention}"
            )
        except discord.HTTPException:
            pass

    # ── Live Update Engine ────────────────────────────────────────────────────
    async def update_see_channel(self, guild: discord.Guild, role: discord.Role):
        """
        Refresh the relevant see-* channel embed when a role is assigned or removed.
        Called from commands.py after /ass and from reaction listeners.
        """
        state = self.bot.state
        role_to_channel = {
            "ORG":             ("see_org", "see_org_msg"),
            "CAP":             ("see_cap", "see_cap_msg"),
            "Invited Adj":     ("see_invited_adjudicators", "see_invited_adjudicators_msg"),
            "Independent Adj": ("see_independent_adjudicators", "see_independent_adjudicators_msg"),
            "Debater":         ("see_debaters", "see_debaters_msg"),
        }
        mapping = None
        for key, val in role_to_channel.items():
            if role.name.startswith(key):
                mapping = val
                break
        if not mapping:
            return

        ch_key, msg_key = mapping
        ch_id = state["channels"].get(ch_key)
        msg_id = state["channels"].get(msg_key)
        if not ch_id or not msg_id:
            return

        ch = guild.get_channel(ch_id)
        if not ch:
            return

        try:
            msg = await ch.fetch_message(msg_id)
        except discord.NotFound:
            return

        existing_embed = msg.embeds[0] if msg.embeds else discord.Embed(title=role.name)
        timestamp = discord.utils.utcnow().strftime("%d-%m-%Y-%H:%M:%S")
        members_with_role = [m for m in guild.members if role in m.roles]
        lines = [f"<@{m.id}> — {timestamp}" for m in members_with_role]
        new_desc = "\n".join(lines) if lines else "_No members assigned yet._"

        new_embed = discord.Embed(
            title=existing_embed.title,
            description=new_desc,
            color=existing_embed.color or discord.Color.blue(),
        )
        await msg.edit(embed=new_embed)

    # ── GA voice session tracking ─────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        state = self.bot.state
        ga_vc_id = state["channels"].get("ga_voice")

        if after.channel and after.channel.id == ga_vc_id:
            state["ga_sessions"][member.id] = {
                "join": discord.utils.utcnow(),
                "exit": None,
            }
            await self._refresh_ga_logs_embed(member.guild)

        elif before.channel and before.channel.id == ga_vc_id:
            if member.id in state["ga_sessions"]:
                state["ga_sessions"][member.id]["exit"] = discord.utils.utcnow()
            await self._refresh_ga_logs_embed(member.guild)

    async def _refresh_ga_logs_embed(self, guild: discord.Guild):
        """Update the ga-logs session embed with current participant count."""
        state = self.bot.state
        ch_id = state["channels"].get("ga_logs")
        msg_id = state["channels"].get("ga_logs_msg")
        if not ch_id or not msg_id:
            return

        ch = guild.get_channel(ch_id)
        if not ch:
            return

        try:
            msg = await ch.fetch_message(msg_id)
        except discord.NotFound:
            return

        active = sum(1 for s in state["ga_sessions"].values() if s["exit"] is None)
        embed = discord.Embed(
            title="🎙️ Grand Auditorium — Session Log",
            description=(
                f"**Currently active:** {active} participant(s)\n"
                f"**Total tracked:** {len(state['ga_sessions'])}\n\n"
                "Press **👥 See Who** for detailed entry/exit/duration data."
            ),
            color=discord.Color.og_blurple(),
            timestamp=discord.utils.utcnow(),
        )
        view = SeeWhoView(self.bot, category_id=None)
        await msg.edit(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(BaseBuilder(bot))
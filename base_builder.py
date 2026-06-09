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
  Reaction-role events log to #assign and #server-audit-logs with format:
    🤖 [Self Assign] @user assigned role @role

Single-Role Enforcement:
  একজন মেম্বার একসাথে শুধুমাত্র একটিই reaction role রাখতে পারবেন।
  নতুন role react করলে আগের reaction role স্বয়ংক্রিয়ভাবে সরে যাবে।
"""

import asyncio
from datetime import datetime, timezone
import discord
from discord.ext import commands


# ── reaction-role ম্যাপিং ───────────────────────────────────────────────────
REACTION_ROLE_MAP = {
    "🟢": "Invited Adj",
    "🔴": "Independent Adj",
    "⚫": "Debater",
    "⚪": "Visitor",
}

HIGH_COMMAND_KEYS = ["ORG", "CAP", "Tabby", "Equity Officer"]


def _get_role(guild: discord.Guild, state: dict, key: str) -> discord.Role | None:
    roles_dict = state.get("roles", {})
    role_id = roles_dict.get(key)
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


# ── Grand Auditorium ও রুম লগ ভিউ ──────────────────────────────────────────────
class SeeWhoView(discord.ui.View):
    """GA/রুম সেশন এম্বেডের সাথে যুক্ত পারসিস্টেন্ট বাটন।"""

    def __init__(self, bot: commands.Bot, category_id: int | None = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.category_id = category_id

    @discord.ui.button(label="👥 See Who", style=discord.ButtonStyle.secondary, custom_id="see_who")
    async def see_who(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.category_id is None:
            sessions = getattr(self.bot, "state", {}).get("ga_sessions", {})
        else:
            sessions = getattr(self.bot, "state", {}).get("room_sessions", {}).get(self.category_id, {})

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


# ── সেটিংস ড্যাশবোর্ড ভিউ ─────────────────────────────────────────────────────
class SettingsDashboardView(discord.ui.View):
    """
    🖥️︱settings চ্যানেলে পোস্ট করা ইন্টারেক্টিভ প্যানেল।
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
            state = getattr(self.bot, "state", {})
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

        state = getattr(self.bot, "state", {})
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

        state = getattr(self.bot, "state", {})
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
        state = getattr(self.bot, "state", {})
        roles_dict = state.get("roles", {})
        restricted_role_keys = ["ORG", "CAP", "Tabby"]
        user_role_ids = {r.id for r in interaction.user.roles}
        for key in restricted_role_keys:
            role_id = roles_dict.get(key)
            if role_id and role_id in user_role_ids:
                return True
        return False


class BaseBuilder(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not hasattr(self.bot, "state"):
            self.bot.state = {}

    # ── Rate-limit হ্যান্ডলিং চ্যানেল মেকার ──────────────────────────────────────────
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

    # ── মূল বেস স্ট্রাকচার নির্মাণ ────────────────────────────────────────────────
    async def build_base(self, guild: discord.Guild, roles: dict):
        """SetupNuke থেকে রোলস তৈরির পর এই মেথডটি কল হয়।"""
        state = self.bot.state

        state.setdefault("channels", {})
        state.setdefault("ga_sessions", {})
        state.setdefault("roles", {})

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

        # ── 👋︱meet-the-developer (position=0) ──────────────────────────────────
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
        dev_view.add_item(discord.ui.Button(
            label="Create Your Own Server",
            url="https://kamla-bot.pages.dev",
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

        # ── Audit log channel (হাই-কমান্ড এক্সেস) ─────────────────────────────
        audit_overwrites = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in high_command:
            audit_overwrites[r] = discord.PermissionOverwrite(
                view_channel=True, send_messages=False, read_message_history=True
            )
        audit_ch = await self._make_channel(guild, "server-audit-logs", None, audit_overwrites)
        state["channels"]["audit_logs"] = audit_ch.id

        # ── জেনারেল চ্যানেলসমূহ ───────────────────────────────────────────────
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

        # ── ফান্ডরেইজার এসেটস ─────────────────────────────────────────────────
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

        # ── ইনফরমেশন হাব ক্যাটাগরি ─────────────────────────────────────────────
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

        # ── গ্র্যান্ড অডিটোরিয়াম ক্যাটাগরি ─────────────────────────────────────────
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

        # ── Grand Auditorium ভয়েস চ্যানেল — কোনো রোলেরই আলাদা restriction নেই ──
        # everyone-এর ডিফল্ট পার্মিশনই সবার জন্য প্রযোজ্য।
        ga_voice_ow = {
            everyone: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True),
        }
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

        # ── কন্ট্রোল সেন্টার ক্যাটাগরি ───────────────────────────────────────────
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

        # ── 🖥️︱settings ড্যাশবোর্ড ──────────────────────────────────────────
        settings_ow = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in [org, cap, tabby]:
            if r:
                settings_ow[r] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=False, read_message_history=True
                )
        settings_ch = await self._make_channel(guild, "🖥️︱settings", orgcom_cat, settings_ow)
        state["channels"]["settings"] = settings_ch.id

        await self._post_settings_dashboard(settings_ch)

    # ── ড্যাশবোর্ড মেসেজ পোস্টার ────────────────────────────────────────────────
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

    # ── নির্দেশিকা মেসেজ পোস্টার ───────────────────────────────────────────────
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
                "#ga-logs tracks session activity."
            ),
            inline=False,
        )
        embed.set_footer(text="KAMLABot — Tournament Management System")
        await channel.send(embed=embed)

    # ── Reaction Role: Single-Role Enforcement সহ ────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        state = getattr(self.bot, "state", {})
        channels_dict = state.get("channels", {})

        if payload.message_id != channels_dict.get("get_role_msg"):
            return
        if payload.user_id == self.bot.user.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        # ১০০% মেম্বার প্রাপ্তি নিশ্চিত করতে fetch_member ব্যবহার
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return

        emoji = payload.emoji.name
        role_display = REACTION_ROLE_MAP.get(emoji)
        if not role_display:
            return

        new_role = discord.utils.find(lambda r: r.name.startswith(role_display), guild.roles)
        if not new_role:
            return

        # ── Single-Role Enforcement ─────────────────────────────────────────────
        # নতুন রোল দেওয়ার আগে মেম্বারের কাছে থাকা যেকোনো reaction role সরানো হচ্ছে।
        for prev_role_prefix in REACTION_ROLE_MAP.values():
            prev_role = discord.utils.find(
                lambda r: r.name.startswith(prev_role_prefix), guild.roles
            )
            if prev_role and prev_role in member.roles and prev_role != new_role:
                await member.remove_roles(
                    prev_role,
                    reason="KAMLABot: single-role enforcement — replacing with new reaction role",
                )
                await asyncio.sleep(0.5)
                await self._log_reaction_event(guild, member, prev_role, action="REMOVE")

        # ── নতুন রোল অ্যাসাইন ─────────────────────────────────────────────────
        await member.add_roles(new_role, reason="KAMLABot reaction role assignment")
        await asyncio.sleep(0.5)

        await self._log_reaction_event(guild, member, new_role, action="ASSIGN")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        state = getattr(self.bot, "state", {})
        channels_dict = state.get("channels", {})

        if payload.message_id != channels_dict.get("get_role_msg"):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return

        emoji = payload.emoji.name
        role_display = REACTION_ROLE_MAP.get(emoji)
        if not role_display:
            return

        role = discord.utils.find(lambda r: r.name.startswith(role_display), guild.roles)
        if role and role in member.roles:
            await member.remove_roles(role, reason="KAMLABot reaction role removal")
            await asyncio.sleep(0.5)

            await self._log_reaction_event(guild, member, role, action="REMOVE")

    async def _log_reaction_event(
        self,
        guild: discord.Guild,
        member: discord.Member,
        role: discord.Role,
        action: str,
    ):
        """
        রিঅ্যাকশন রোল ইভেন্টগুলো #assign ও #server-audit-logs চ্যানেলে লগ করে।
        """
        state = getattr(self.bot, "state", {})
        channels_dict = state.get("channels", {})

        assign_ch_id = channels_dict.get("assign")
        audit_ch_id = channels_dict.get("audit_logs")

        if action == "ASSIGN":
            raw_msg = f"🤖 [Auto Sync] `/ass` **ASSIGN** to:{member.mention} as:{role.mention}"
            fancy_embed = discord.Embed(
                title="🟢 Self-Reaction Role Assigned",
                description=(
                    f"মেম্বার {member.mention} (`{member.name}`) সেলফ রিয়্যাকশন রোল থেকে "
                    f"সফলভাবে {role.mention} রোলটি নিজের আইডিতে অ্যাসাইন করেছেন।"
                ),
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
        else:
            raw_msg = f"🤖 [Auto Sync] `/ass` **REMOVE** to:{member.mention} as:{role.mention}"
            fancy_embed = discord.Embed(
                title="🔴 Self-Reaction Role Removed",
                description=(
                    f"মেম্বার {member.mention} (`{member.name}`) সেলফ রিয়্যাকশন রোল থেকে "
                    f"{role.mention} রোলটি রিমুভ করেছেন।"
                ),
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )

        fancy_embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        fancy_embed.set_footer(text=f"User ID: {member.id} | Role ID: {role.id}")

        # ১. #assign চ্যানেলে র লগ
        if assign_ch_id:
            assign_ch = guild.get_channel(assign_ch_id)
            if assign_ch:
                try:
                    await assign_ch.send(raw_msg)
                except discord.HTTPException:
                    pass

        # ২. #server-audit-logs চ্যানেলে ফ্যান্সি এম্বেড লগ
        if audit_ch_id:
            audit_ch = guild.get_channel(audit_ch_id)
            if audit_ch:
                try:
                    await audit_ch.send(embed=fancy_embed)
                except discord.HTTPException:
                    pass

    # ── GA ভয়েস সেশন ট্র্যাকিং ────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        state = getattr(self.bot, "state", {})
        ga_vc_id = state.get("channels", {}).get("ga_voice")

        if not ga_vc_id:
            return

        sessions = state.get("ga_sessions", {})

        if after.channel and after.channel.id == ga_vc_id:
            sessions[member.id] = {
                "join": discord.utils.utcnow(),
                "exit": None,
            }
            state["ga_sessions"] = sessions
            await self._refresh_ga_logs_embed(member.guild)

        elif before.channel and before.channel.id == ga_vc_id:
            if member.id in sessions:
                sessions[member.id]["exit"] = discord.utils.utcnow()
                state["ga_sessions"] = sessions
            await self._refresh_ga_logs_embed(member.guild)

    async def _refresh_ga_logs_embed(self, guild: discord.Guild):
        """GA সেশন লগ আপডেট করে।"""
        state = getattr(self.bot, "state", {})
        channels_dict = state.get("channels", {})

        ch_id = channels_dict.get("ga_logs")
        msg_id = channels_dict.get("ga_logs_msg")
        if not ch_id or not msg_id:
            return

        ch = guild.get_channel(ch_id)
        if not ch:
            return

        try:
            msg = await ch.fetch_message(msg_id)
        except discord.NotFound:
            return

        sessions = state.get("ga_sessions", {})
        active = sum(1 for s in sessions.values() if s["exit"] is None)
        embed = discord.Embed(
            title="🎙️ Grand Auditorium — Session Log",
            description=(
                f"**Currently active:** {active} participant(s)\n"
                f"**Total tracked:** {len(sessions)}\n\n"
                "Press **👥 See Who** for detailed entry/exit/duration data."
            ),
            color=discord.Color.og_blurple(),
            timestamp=discord.utils.utcnow(),
        )
        view = SeeWhoView(self.bot, category_id=None)
        await msg.edit(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(BaseBuilder(bot))
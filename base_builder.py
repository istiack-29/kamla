"""
KAMLABot — base_builder.py
Cog: BaseBuilder

Creates global infrastructure after /startb nuke:
    • 👋︱meet-the-developer       (top, read-only, Instagram + Donate buttons)
    • #server-audit-logs           (high command only)
    • General Assets               (welcome, get-role, assign, how-to-use)
    • Fundraiser Assets            (only if tournament_type == 2)
    • Information Hub category
    • Grand Auditorium category    (with ga-logs + See Who view)
    • OrgCom Control Center        (with 🖥️︱settings dashboard)

The Settings dashboard exposes 4 buttons:
    ➕ Add Room       — RoomEngine.add_room()
    ➖ Remove Room    — RoomEngine.remove_room()
    🔄 Switch Format  — RoomEngine.swap_format() (AP ⇄ BP, hot)
    💸 Enable Fundraiser — one-way toggle, builds Transparency category
"""

import asyncio
import discord
from discord.ext import commands


RATE_SLEEP = 1.2
HC_KEYS = ("ORG", "CAP", "Tabby", "Equity Officer")

REACTION_ROLE_MAP = {
    "🟢": "Invited Adj",
    "🔴": "Independent Adj",
    "⚫": "Debater",
    "⚪": "Visitor",
}

INSTAGRAM_URL = "https://instagram.com/"
DONATE_URL = "https://example.com/donate"


def _hc_overwrites(guild, state):
    """View-only for everyone else, RW for high command."""
    everyone = guild.default_role
    ow = {everyone: discord.PermissionOverwrite(view_channel=False)}
    for k in HC_KEYS:
        rid = state["roles"].get(k)
        r = guild.get_role(rid) if rid else None
        if r:
            ow[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    return ow


def _read_only_public(guild):
    everyone = guild.default_role
    return {everyone: discord.PermissionOverwrite(
        view_channel=True, send_messages=False, read_message_history=True, add_reactions=True
    )}


# ── Persistent views ─────────────────────────────────────────────────────────
class MeetDevView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="📸 Instagram", url=INSTAGRAM_URL, style=discord.ButtonStyle.link))
        self.add_item(discord.ui.Button(label="💖 Donate", url=DONATE_URL, style=discord.ButtonStyle.link))


class SeeWhoView(discord.ui.View):
    """Ephemeral 'See Who' button for GA / room session logs."""
    def __init__(self, bot: commands.Bot, category_id: int | None = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.category_id = category_id

    @discord.ui.button(label="👥 See Who", style=discord.ButtonStyle.secondary, custom_id="see_who")
    async def see_who(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        state = self.bot.state
        if self.category_id is None:
            sessions = state.get("ga_sessions", {})
        else:
            sessions = state.get("room_sessions", {}).get(self.category_id, {})
        if not sessions:
            await interaction.response.send_message("No sessions recorded yet.", ephemeral=True)
            return
        lines = []
        for uid, data in sessions.items():
            mem = interaction.guild.get_member(uid)
            who = mem.mention if mem else f"<@{uid}>"
            join = data.get("join")
            exit_ = data.get("exit")
            lines.append(f"• {who} — joined {discord.utils.format_dt(join, 'T') if join else '?'}"
                         f"{' • left ' + discord.utils.format_dt(exit_, 'T') if exit_ else ' • still in'}")
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


class SettingsDashboardView(discord.ui.View):
    """🖥️ Restricted Settings panel — ORG / CAP / Tabby only."""
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _gate(self, interaction: discord.Interaction) -> bool:
        state = self.bot.state
        allowed_ids = {state["roles"].get(k) for k in ("ORG", "CAP", "Tabby")}
        allowed_ids.discard(None)
        if not any(r.id in allowed_ids for r in interaction.user.roles):
            await interaction.response.send_message("❌ Restricted to ORG / CAP / Tabby.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="➕ Add Room", style=discord.ButtonStyle.success, custom_id="dash_add")
    async def add_room(self, interaction: discord.Interaction, _btn):
        if not await self._gate(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        engine = self.bot.get_cog("RoomEngine")
        if not engine:
            await interaction.followup.send("RoomEngine missing.", ephemeral=True); return
        idx = await engine.add_room(interaction.guild)
        await interaction.followup.send(f"✅ Spawned **Room {idx}**.", ephemeral=True)

    @discord.ui.button(label="➖ Remove Room", style=discord.ButtonStyle.danger, custom_id="dash_remove")
    async def remove_room(self, interaction: discord.Interaction, _btn):
        if not await self._gate(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        engine = self.bot.get_cog("RoomEngine")
        if not engine:
            await interaction.followup.send("RoomEngine missing.", ephemeral=True); return
        idx = await engine.remove_room(interaction.guild)
        if idx is None:
            await interaction.followup.send("⚠️ No rooms left.", ephemeral=True)
        else:
            await interaction.followup.send(f"🗑️ Removed **Room {idx}**.", ephemeral=True)

    @discord.ui.button(label="🔄 Switch Format", style=discord.ButtonStyle.primary, custom_id="dash_swap")
    async def swap_format(self, interaction: discord.Interaction, _btn):
        if not await self._gate(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        engine = self.bot.get_cog("RoomEngine")
        if not engine:
            await interaction.followup.send("RoomEngine missing.", ephemeral=True); return
        current = self.bot.state.get("format") or 1
        new_fmt = 2 if current == 1 else 1
        await engine.swap_format(interaction.guild, new_fmt)
        label = "BP" if new_fmt == 2 else "AP"
        await interaction.followup.send(f"🔄 All rooms migrated to **{label}**.", ephemeral=True)

    @discord.ui.button(label="💸 Enable Fundraiser", style=discord.ButtonStyle.secondary, custom_id="dash_fund")
    async def fundraiser(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await self._gate(interaction):
            return
        if self.bot.state.get("fundraiser_enabled"):
            await interaction.response.send_message("Already enabled.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        base = self.bot.get_cog("BaseBuilder")
        await base.build_fundraiser_category(interaction.guild)
        self.bot.state["fundraiser_enabled"] = True
        btn.disabled = True
        btn.label = "💸 Fundraiser Enabled"
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send("💸 Transparency category injected.", ephemeral=True)


# ── Cog ──────────────────────────────────────────────────────────────────────
class BaseBuilder(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._views_registered = False

    async def cog_load(self):
        if not self._views_registered:
            self.bot.add_view(MeetDevView())
            self.bot.add_view(SettingsDashboardView(self.bot))
            self.bot.add_view(SeeWhoView(self.bot))
            self._views_registered = True

    # ── helpers ──────────────────────────────────────────────────────────────
    async def _sleep(self):
        await asyncio.sleep(RATE_SLEEP)

    def _roles(self, guild):
        state = self.bot.state
        return {k: guild.get_role(rid) for k, rid in state["roles"].items()}

    # ── public entry point called by SetupNuke ───────────────────────────────
    async def build_all(self, guild: discord.Guild, roles: dict, term=None):
        async def tlog(line, level="build"):
            if term:
                await term.log(line, level)

        # 1) audit logs
        ow = _hc_overwrites(guild, self.bot.state)
        await self._sleep()
        audit = await guild.create_text_channel("server-audit-logs", overwrites=ow)
        self.bot.state["channels"]["audit_logs"] = audit.id
        await tlog("[BUILD] #server-audit-logs")

        # 2) meet-the-developer (top, position 0, read-only)
        meet_ow = _read_only_public(guild)
        await self._sleep()
        meet = await guild.create_text_channel("👋︱meet-the-developer", overwrites=meet_ow, position=0)
        self.bot.state["channels"]["meet_dev"] = meet.id
        try:
            kwargs = {"view": MeetDevView(),
                      "content": "## 👋 Meet the Developer\nBuilt by KAMLABot. Links below."}
            try:
                f = discord.File("m.png")
                kwargs["file"] = f
            except FileNotFoundError:
                pass
            await meet.send(**kwargs)
        except discord.HTTPException:
            pass
        await tlog("[BUILD] 👋︱meet-the-developer")

        # 3) General Assets category
        await self._sleep()
        gen = await guild.create_category("📂 General Assets")
        self.bot.state["channels"]["general_cat"] = gen.id

        await self._sleep()
        welcome = await guild.create_text_channel("👋︱welcome", category=gen,
                                                  overwrites=_read_only_public(guild))
        self.bot.state["channels"]["welcome"] = welcome.id

        await self._sleep()
        get_role = await guild.create_text_channel("🙉︱get-role", category=gen,
                                                   overwrites=_read_only_public(guild))
        self.bot.state["channels"]["get_role"] = get_role.id

        role_msg = await get_role.send(
            "## 🎭 Pick your role\n"
            "🟢 Invited Adj\n🔴 Independent Adj\n⚫ Debater\n⚪ Visitor"
        )
        for emoji in REACTION_ROLE_MAP:
            try:
                await role_msg.add_reaction(emoji)
            except discord.HTTPException:
                pass
        self.bot.state["channels"]["get_role_msg"] = role_msg.id

        await self._sleep()
        assign = await guild.create_text_channel("📝︱assign", category=gen,
                                                 overwrites=_hc_overwrites(guild, self.bot.state))
        self.bot.state["channels"]["assign"] = assign.id

        await self._sleep()
        how = await guild.create_text_channel("📘︱how-to-use", category=gen,
                                              overwrites=_read_only_public(guild))
        self.bot.state["channels"]["how_to_use"] = how.id
        await how.send("## 📘 How to use\nUse `/ass`, `/rate`, `/allin`, and `.7m` timers.")

        await tlog("[BUILD] General Assets")

        # 4) Fundraiser
        if self.bot.state.get("tournament_type") == 2:
            await self.build_fundraiser_category(guild)
            self.bot.state["fundraiser_enabled"] = True
            await tlog("[BUILD] Fundraiser (Transparency)")

        # 5) Information Hub
        await self._sleep()
        info = await guild.create_category("📚 Information Hub")
        self.bot.state["channels"]["info_cat"] = info.id
        for name in ("📜︱rules", "📅︱schedule", "🎯︱motions-archive", "🏆︱results"):
            await self._sleep()
            await guild.create_text_channel(name, category=info, overwrites=_read_only_public(guild))

        # 6) Grand Auditorium
        ga_ow = {guild.default_role: discord.PermissionOverwrite(view_channel=True)}
        await self._sleep()
        ga_cat = await guild.create_category("🎭 Grand Auditorium", overwrites=ga_ow)
        self.bot.state["channels"]["ga_cat"] = ga_cat.id
        await self._sleep()
        ga_stage = await guild.create_voice_channel("🎤 GA Stage", category=ga_cat)
        self.bot.state["channels"]["ga_stage"] = ga_stage.id
        await self._sleep()
        ga_motion = await guild.create_text_channel("🎯︱motion", category=ga_cat,
                                                    overwrites=_read_only_public(guild))
        self.bot.state["channels"]["motion"] = ga_motion.id
        await self._sleep()
        ga_logs = await guild.create_text_channel("📊︱ga-logs", category=ga_cat,
                                                  overwrites=_hc_overwrites(guild, self.bot.state))
        self.bot.state["channels"]["ga_logs"] = ga_logs.id
        await ga_logs.send("## 📊 GA Session Log", view=SeeWhoView(self.bot, None))

        await tlog("[BUILD] Grand Auditorium")

        # 7) OrgCom Control Center
        await self._sleep()
        org_cat = await guild.create_category("⚜️︱orgcom", overwrites=_hc_overwrites(guild, self.bot.state))
        self.bot.state["channels"]["org_cat"] = org_cat.id

        await self._sleep()
        chat = await guild.create_text_channel("💬︱orgcom-chat", category=org_cat,
                                               overwrites=_hc_overwrites(guild, self.bot.state))
        self.bot.state["channels"]["orgcom_chat"] = chat.id

        # 🖥️ Settings — restricted to ORG / CAP / Tabby
        settings_ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        for k in ("ORG", "CAP", "Tabby"):
            rid = self.bot.state["roles"].get(k)
            r = guild.get_role(rid) if rid else None
            if r:
                settings_ow[r] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        await self._sleep()
        settings = await guild.create_text_channel("🖥️︱settings", category=org_cat, overwrites=settings_ow)
        self.bot.state["channels"]["settings"] = settings.id

        embed = discord.Embed(
            title="🖥️  KAMLABot Control Dashboard",
            description=(
                "Live runtime morphing. Restricted to **ORG · CAP · Tabby**.\n\n"
                "**➕ Add Room** — spawn a new room category\n"
                "**➖ Remove Room** — delete the highest numbered room\n"
                "**🔄 Switch Format** — hot-swap AP ⇄ BP across all rooms\n"
                "**💸 Enable Fundraiser** — one-way Transparency injection"
            ),
            color=0x00E5FF,
        )
        await settings.send(embed=embed, view=SettingsDashboardView(self.bot))

        await tlog("[BUILD] OrgCom + Settings Dashboard")

    # ── Fundraiser (one-way) ─────────────────────────────────────────────────
    async def build_fundraiser_category(self, guild: discord.Guild):
        await self._sleep()
        cat = await guild.create_category("💸 Transparency",
                                          overwrites=_read_only_public(guild))
        self.bot.state["channels"]["fundraiser_cat"] = cat.id
        for name in ("💰︱donations", "📊︱ledger", "🧾︱receipts", "🙏︱thank-you"):
            await self._sleep()
            await guild.create_text_channel(name, category=cat, overwrites=_read_only_public(guild))

    # ── Ghost Auto-Assign: reaction roles + assign-channel echo ──────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction(payload, add=False)

    async def _handle_reaction(self, payload, *, add: bool):
        if self.bot.state.get("is_setup_active"):
            return
        if payload.message_id != self.bot.state["channels"].get("get_role_msg"):
            return
        emoji = str(payload.emoji)
        role_key = REACTION_ROLE_MAP.get(emoji)
        if not role_key:
            return
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not guild:
            return
        role_id = self.bot.state["roles"].get(role_key)
        role = guild.get_role(role_id) if role_id else None
        if not role:
            return
        member = guild.get_member(payload.user_id)
        if not member or member.bot:
            return
        try:
            if add:
                await member.add_roles(role, reason="Ghost Auto-Assign")
            else:
                await member.remove_roles(role, reason="Ghost Auto-Assign")
        except discord.HTTPException:
            return

        assign_id = self.bot.state["channels"].get("assign")
        ch = guild.get_channel(assign_id) if assign_id else None
        if ch:
            verb = "" if add else " (removed)"
            try:
                await ch.send(f"🤖 [Auto Sync] /ass to:{member.mention} as:{role.mention}{verb}")
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(BaseBuilder(bot))

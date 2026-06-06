"""
KAMLABot — base_builder.py
Cog: BaseBuilder

Handles creation of:
  • Global Audit Log channel (#server-audit-logs)
  • General Assets (welcome, get-role, assign, how-to-use)
  • Fundraiser Assets (if tournament_type == 2)
  • Information Hub category
  • Grand Auditorium category  (with #ga-logs button UI)
  • OrgCom Control Center category
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
    """Look up a role by internal key via the state dict."""
    role_id = state["roles"].get(key)
    return guild.get_role(role_id) if role_id else None


def _allow(*roles: discord.Role | None) -> list[tuple]:
    """Return (role, overwrite) pairs that grant basic read+send access."""
    overwrite = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
    )
    return [(r, overwrite) for r in roles if r]


def _read_only(*roles: discord.Role | None) -> list[tuple]:
    """Return (role, overwrite) pairs that grant read-only access."""
    overwrite = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=False,
        read_message_history=True,
    )
    return [(r, overwrite) for r in roles if r]


def _deny(role: discord.Role | None) -> tuple | None:
    """Return an overwrite that hides a channel from a role."""
    if not role:
        return None
    return (role, discord.PermissionOverwrite(view_channel=False))


# ── "See Who" ephemeral view for GA logs ──────────────────────────────────────
class SeeWhoView(discord.ui.View):
    """Persistent button attached to GA session embeds."""

    def __init__(self, bot: commands.Bot, category_id: int | None = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.category_id = category_id  # None = GA, int = room category

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
        """
        Creates a text or voice channel with rate-limit evasion (1.5 s sleep).
        """
        await asyncio.sleep(1.5)  # Rate-limit evasion — prevents HTTP 429
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
        await asyncio.sleep(1.5)  # Rate-limit evasion
        return await guild.create_category(name, overwrites=overwrites)

    # ── Build entry point ─────────────────────────────────────────────────────
    async def build_base(self, guild: discord.Guild, roles: dict):
        """Called by SetupNuke after roles are created."""
        state = self.bot.state

        # Convenience role lookups
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

        # ── Audit log channel (no category, high-command only) ────────────────
        audit_overwrites = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in high_command:
            audit_overwrites[r] = discord.PermissionOverwrite(
                view_channel=True, send_messages=False, read_message_history=True
            )
        audit_ch = await self._make_channel(
            guild, "server-audit-logs", None, audit_overwrites
        )
        state["channels"]["audit_logs"] = audit_ch.id

        # ── General Assets ────────────────────────────────────────────────────
        general_ow = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False)}

        # Welcome (read-only for everyone; bot can send)
        welcome_ch = await self._make_channel(guild, "👐🏻︱welcome", None, general_ow)
        state["channels"]["welcome"] = welcome_ch.id

        # Get-role (read-only for everyone; bot posts the reaction embed)
        get_role_ch = await self._make_channel(guild, "🙉︱get-role", None, general_ow)
        state["channels"]["get_role"] = get_role_ch.id

        # Post reaction-role message
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

        # Assign (restricted: only ORG, CAP, Tabby visible)
        assign_ow = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in [org, cap, tabby]:
            if r:
                assign_ow[r] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        assign_ch = await self._make_channel(guild, "😭︱assign", None, assign_ow)
        state["channels"]["assign"] = assign_ch.id

        # How-to-use (global read-only)
        howto_ch = await self._make_channel(guild, "❓︱how-to-use-this-server", None, general_ow)
        state["channels"]["how_to_use"] = howto_ch.id
        await self._post_how_to_use(howto_ch)

        # ── Fundraiser Assets ─────────────────────────────────────────────────
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

        # Read-only channels; ORG/CAP can send
        send_ow = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False)}
        for r in [org, cap]:
            if r:
                send_ow[r] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        for ch_name in ["schedule", "important-forms", "debater-briffing", "equity-briffing", "judge-briffing"]:
            await self._make_channel(guild, ch_name, info_cat, send_ow)

        # Live Update Engine — see-* channels
        see_channels = {
            "see-org":                     "ORG",
            "see-cap":                     "CAP",
            "see-invited-adjudicators":    "Invited Adj",
            "see-independent-adjudicators":"Independent Adj",
            "see-debaters":                "Debater",
        }
        for ch_name, role_key in see_channels.items():
            ch = await self._make_channel(guild, ch_name, info_cat, info_ow)
            state["channels"][ch_name.replace("-", "_")] = ch.id
            # Post an initial embed that the bot will edit on role assignment
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

        # ga-chat: all except Visitors can send
        ga_chat_ow = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
        if visitor:
            ga_chat_ow[visitor] = discord.PermissionOverwrite(
                view_channel=True, send_messages=False
            )
        await self._make_channel(guild, "ga-chat", ga_cat, ga_chat_ow)

        # Read-only; ORG/CAP/Tabby can send
        ga_send_ow = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False)}
        for r in [org, cap, tabby]:
            if r:
                ga_send_ow[r] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        for ch_name in ["motion", "announcement", "break-announcement", "matchup", "ballot"]:
            ch = await self._make_channel(guild, ch_name, ga_cat, ga_send_ow)
            state["channels"][ch_name.replace("-", "_")] = ch.id

        # Grand Auditorium voice: open to all; Visitors are muted + no soundboard
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

        # ga-logs: ORG/Tabby only
        ga_logs_ow = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in [org, tabby]:
            if r:
                ga_logs_ow[r] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        ga_logs_ch = await self._make_channel(guild, "ga-logs", ga_cat, ga_logs_ow)
        state["channels"]["ga_logs"] = ga_logs_ch.id

        # Post initial session embed with "See Who" button
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

    # ── How-to-use content ────────────────────────────────────────────────────
    async def _post_how_to_use(self, channel: discord.TextChannel):
        embed = discord.Embed(
            title="❓ How to Use This Server",
            color=discord.Color.teal(),
        )
        embed.add_field(
            name="📌 Getting Your Role",
            value=(
                "Go to <#> `🙉︱get-role` and react with the emoji that matches you:\n"
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
                "The bot will post and update an ASCII progress bar every 5 seconds."
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

    # ── Reaction-role listener ────────────────────────────────────────────────
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

        emoji = str(payload.emoji)
        role_display = REACTION_ROLE_MAP.get(emoji)
        if not role_display:
            return

        # Find the role by display name
        role = discord.utils.find(lambda r: r.name == role_display, guild.roles)
        if role:
            await member.add_roles(role, reason="KAMLABot reaction role")

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

        emoji = str(payload.emoji)
        role_display = REACTION_ROLE_MAP.get(emoji)
        if not role_display:
            return

        role = discord.utils.find(lambda r: r.name == role_display, guild.roles)
        if role and role in member.roles:
            await member.remove_roles(role, reason="KAMLABot reaction role removal")

    # ── Live Update Engine ────────────────────────────────────────────────────
    async def update_see_channel(self, guild: discord.Guild, role: discord.Role):
        """
        Appended to the relevant see-* channel embed when a role is assigned.
        Fetches the pinned embed message, appends the new user, and edits it.
        Called from commands.py after /ass.
        """
        state = self.bot.state
        role_to_channel = {
            "ORG":           ("see_org", "see_org_msg"),
            "CAP":           ("see_cap", "see_cap_msg"),
            "Invited Adj":   ("see_invited_adjudicators", "see_invited_adjudicators_msg"),
            "Independent Adj": ("see_independent_adjudicators", "see_independent_adjudicators_msg"),
            "Debater":       ("see_debaters", "see_debaters_msg"),
        }
        # Match by role name prefix
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
        old_desc = existing_embed.description or ""
        if old_desc == "_No members assigned yet._":
            old_desc = ""

        timestamp = discord.utils.utcnow().strftime("%d-%m-%Y-%H:%M:%S")
        # Find members with this role and rebuild description (no pings, just mentions)
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

        # GA join
        if after.channel and after.channel.id == ga_vc_id:
            state["ga_sessions"][member.id] = {
                "join": discord.utils.utcnow(),
                "exit": None,
            }
            await self._refresh_ga_logs_embed(member.guild)

        # GA leave
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

        active = sum(
            1 for s in state["ga_sessions"].values() if s["exit"] is None
        )
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

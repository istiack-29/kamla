"""
KAMLABot — room_engine.py
Cog: RoomEngine

Handles algorithm-driven generation of debate room categories.
Each room category contains:
  Text:  timer, poi, logs
  Voice: debate
  Voice (AP): GOV Prep (max 3), OPP Prep (max 3), Judgment
  Voice (BP): OG Prep (max 2), OO Prep (max 2), CG Prep (max 2), CO Prep (max 2), Judgment

Rate-limit evasion: await asyncio.sleep(1.5) between EVERY channel/category creation.

Visitor restriction logic:
  Visitors can ONLY see and join the 'debate' voice channel per room.
  All other room channels are hidden from Visitors.
"""

import asyncio
import discord
from discord.ext import commands

from base_builder import SeeWhoView


class RoomEngine(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _make(
        self,
        guild: discord.Guild,
        name: str,
        category: discord.CategoryChannel,
        overwrites: dict,
        channel_type: str = "text",
        user_limit: int = 0,
    ) -> discord.abc.GuildChannel:
        """
        Single channel factory with mandatory rate-limit sleep.
        Every call sleeps 1.5 s BEFORE creation to respect Discord's
        rate limit on bulk channel creation (HTTP 429 avoidance).
        """
        await asyncio.sleep(1.5)  # Rate-limit evasion — DO NOT REMOVE
        if channel_type == "voice":
            return await guild.create_voice_channel(
                name,
                category=category,
                overwrites=overwrites,
                user_limit=user_limit if user_limit else 0,
            )
        return await guild.create_text_channel(
            name,
            category=category,
            overwrites=overwrites,
        )

    async def build_rooms(
        self,
        guild: discord.Guild,
        roles: dict,
        debate_format: int,
        num_rooms: int,
    ):
        """
        Main entry point. Creates `num_rooms` room categories sequentially.
        Sequential (not parallel) to avoid rate-limit spikes.
        """
        state = self.bot.state
        everyone = guild.default_role
        visitor = roles.get("Visitor")
        org = roles.get("ORG")
        cap = roles.get("CAP")
        tabby = roles.get("Tabby")
        equity = roles.get("Equity Officer")
        invited_adj = roles.get("Invited Adj")
        indep_adj = roles.get("Independent Adj")
        debater = roles.get("Debater")

        # High-command can see everything
        high_command = [r for r in [org, cap, tabby, equity] if r]
        # All non-visitor roles can see room channels
        participant_roles = [r for r in [org, cap, tabby, equity, invited_adj, indep_adj, debater] if r]

        # State: room_sessions indexed by category_id
        state["room_sessions"] = {}
        state["room_channel_map"] = {}  # category_id -> {poi, debate, prep_vcs, judgment}

        for room_num in range(1, num_rooms + 1):
            await self._build_single_room(
                guild=guild,
                room_num=room_num,
                debate_format=debate_format,
                everyone=everyone,
                visitor=visitor,
                high_command=high_command,
                participant_roles=participant_roles,
                state=state,
            )

    async def _build_single_room(
        self,
        guild: discord.Guild,
        room_num: int,
        debate_format: int,
        everyone: discord.Role,
        visitor: discord.Role | None,
        high_command: list,
        participant_roles: list,
        state: dict,
    ):
        """Create one room category and all its channels."""

        # ── Category overwrites ───────────────────────────────────────────────
        # @everyone hidden by default; participants can see; Visitor hidden here
        cat_ow = {
            everyone: discord.PermissionOverwrite(view_channel=False),
        }
        for r in participant_roles:
            cat_ow[r] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                connect=True,
                speak=True,
            )
        if visitor:
            # Visitor: hidden at category level; overridden per debate VC
            cat_ow[visitor] = discord.PermissionOverwrite(view_channel=False)

        # Rate-limit evasion: sleep before category creation too
        await asyncio.sleep(1.5)
        category = await guild.create_category(
            f"ROOM {room_num}",
            overwrites=cat_ow,
        )

        # ── Visitor-only debate-VC overwrite ──────────────────────────────────
        # Base text-channel overwrite (no Visitor access)
        text_ow = dict(cat_ow)

        # Debate VC: Visitor CAN see + join this one channel
        debate_ow = dict(cat_ow)
        if visitor:
            debate_ow[visitor] = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=False,          # Visitors are listen-only in debate VC
                use_soundboard=False,
            )

        # ── Text channels ─────────────────────────────────────────────────────
        timer_ch = await self._make(guild, "timer", category, text_ow)
        poi_ch = await self._make(guild, "poi", category, text_ow)
        logs_ch = await self._make(guild, "logs", category, text_ow)

        # ── Logs: post initial session embed with "See Who" button ────────────
        state["room_sessions"][category.id] = {}
        session_embed = discord.Embed(
            title=f"🔊 ROOM {room_num} — Session Log",
            description="No voice activity yet.",
            color=discord.Color.dark_teal(),
        )
        view = SeeWhoView(self.bot, category_id=category.id)
        logs_msg = await logs_ch.send(embed=session_embed, view=view)

        # ── Voice: debate ─────────────────────────────────────────────────────
        debate_vc = await self._make(
            guild, "debate", category, debate_ow, channel_type="voice"
        )

        # ── Format-specific prep rooms ────────────────────────────────────────
        prep_vcs = []
        judgment_vc = None

        if debate_format == 1:
            # AP format
            gov_prep = await self._make(
                guild, "GOV Prep", category, text_ow,
                channel_type="voice", user_limit=3,
            )
            opp_prep = await self._make(
                guild, "OPP Prep", category, text_ow,
                channel_type="voice", user_limit=3,
            )
            judgment_vc = await self._make(
                guild, "Judgment", category, text_ow, channel_type="voice"
            )
            prep_vcs = [gov_prep, opp_prep]

        else:
            # BP format
            og_prep = await self._make(
                guild, "OG Prep", category, text_ow,
                channel_type="voice", user_limit=2,
            )
            oo_prep = await self._make(
                guild, "OO Prep", category, text_ow,
                channel_type="voice", user_limit=2,
            )
            cg_prep = await self._make(
                guild, "CG Prep", category, text_ow,
                channel_type="voice", user_limit=2,
            )
            co_prep = await self._make(
                guild, "CO Prep", category, text_ow,
                channel_type="voice", user_limit=2,
            )
            judgment_vc = await self._make(
                guild, "Judgment", category, text_ow, channel_type="voice"
            )
            prep_vcs = [og_prep, oo_prep, cg_prep, co_prep]

        # ── Store channel map for /allin and voice tracking ───────────────────
        state["room_channel_map"][category.id] = {
            "poi": poi_ch.id,
            "timer": timer_ch.id,
            "logs": logs_ch.id,
            "logs_msg": logs_msg.id,
            "debate_vc": debate_vc.id,
            "prep_vcs": [vc.id for vc in prep_vcs],
            "judgment_vc": judgment_vc.id if judgment_vc else None,
        }

    # ── Room voice session tracking ───────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        state = self.bot.state
        room_map = state.get("room_channel_map", {})

        def find_room(channel: discord.VoiceChannel | None):
            if not channel:
                return None
            for cat_id, data in room_map.items():
                all_vcs = [data["debate_vc"]] + data["prep_vcs"]
                if data.get("judgment_vc"):
                    all_vcs.append(data["judgment_vc"])
                if channel.id in all_vcs:
                    return cat_id
            return None

        joined_room = find_room(after.channel)
        left_room = find_room(before.channel)

        if joined_room and joined_room != left_room:
            state["room_sessions"].setdefault(joined_room, {})
            state["room_sessions"][joined_room][member.id] = {
                "join": discord.utils.utcnow(),
                "exit": None,
            }
            await self._refresh_room_log(member.guild, joined_room)

        if left_room and left_room != joined_room:
            sessions = state["room_sessions"].get(left_room, {})
            if member.id in sessions and sessions[member.id]["exit"] is None:
                sessions[member.id]["exit"] = discord.utils.utcnow()
            await self._refresh_room_log(member.guild, left_room)

    async def _refresh_room_log(self, guild: discord.Guild, category_id: int):
        """Update the room's logs channel embed."""
        state = self.bot.state
        room_data = state.get("room_channel_map", {}).get(category_id)
        if not room_data:
            return

        logs_ch = guild.get_channel(room_data["logs"])
        if not logs_ch:
            return

        try:
            logs_msg = await logs_ch.fetch_message(room_data["logs_msg"])
        except discord.NotFound:
            return

        sessions = state["room_sessions"].get(category_id, {})
        active = sum(1 for s in sessions.values() if s["exit"] is None)

        category = guild.get_channel(category_id)
        room_name = category.name if category else f"Room {category_id}"

        embed = discord.Embed(
            title=f"🔊 {room_name} — Session Log",
            description=(
                f"**Currently active:** {active} participant(s)\n"
                f"**Total tracked:** {len(sessions)}\n\n"
                "Press **👥 See Who** for detailed entry/exit/duration data."
            ),
            color=discord.Color.dark_teal(),
            timestamp=discord.utils.utcnow(),
        )
        view = SeeWhoView(self.bot, category_id=category_id)
        await logs_msg.edit(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(RoomEngine(bot))

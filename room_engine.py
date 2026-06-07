"""
KAMLABot — room_engine.py
Cog: RoomEngine

Handles algorithm-driven generation of debate room categories.
Each room category contains:
  Text:  timer, poi, logs
  Voice: debate
  Voice (AP): GOV Prep (max 3), OPP Prep (max 3), Judgment
  Voice (BP): OG Prep (max 2), OO Prep (max 2), CG Prep (max 2), CO Prep (max 2), Judgment

Rate-limit evasion: await asyncio.sleep(1.2) between EVERY channel/category creation.

Abstract Room Lifecycle Factory:
  - spawn_room()   — independently create a new numbered room at runtime
  - remove_room()  — destroy the highest-numbered room category at runtime
  - switch_format() — hot-swap all active rooms between AP and BP format

Hot-Swap Format Matrix (AP ⇄ BP):
  CRITICAL — #timer, #poi, #logs, and the primary 'debate' voice channel
  are NEVER touched during conversion. Only format-specific prep VCs are
  deleted and re-injected.

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

    # ── Rate-limited channel factory ──────────────────────────────────────────
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
        Every call sleeps 1.2 s BEFORE creation to respect Discord's
        rate limit on bulk channel creation (HTTP 429 avoidance).
        """
        await asyncio.sleep(1.2)
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

    # ── Build all rooms (initial setup) ───────────────────────────────────────
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

        high_command = [r for r in [org, cap, tabby, equity] if r]
        participant_roles = [r for r in [org, cap, tabby, equity, invited_adj, indep_adj, debater] if r]

        state["room_sessions"] = {}
        state["room_channel_map"] = {}

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

    # ── Single room builder ───────────────────────────────────────────────────
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
            cat_ow[visitor] = discord.PermissionOverwrite(view_channel=False)

        await asyncio.sleep(1.2)
        category = await guild.create_category(
            f"ROOM {room_num}",
            overwrites=cat_ow,
        )

        text_ow = dict(cat_ow)

        debate_ow = dict(cat_ow)
        if visitor:
            debate_ow[visitor] = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=False,
                use_soundboard=False,
            )

        timer_ch = await self._make(guild, "timer", category, text_ow)
        poi_ch = await self._make(guild, "poi", category, text_ow)
        logs_ch = await self._make(guild, "logs", category, text_ow)

        state["room_sessions"][category.id] = {}
        session_embed = discord.Embed(
            title=f"🔊 ROOM {room_num} — Session Log",
            description="No voice activity yet.",
            color=discord.Color.dark_teal(),
        )
        view = SeeWhoView(self.bot, category_id=category.id)
        logs_msg = await logs_ch.send(embed=session_embed, view=view)

        debate_vc = await self._make(
            guild, "debate", category, debate_ow, channel_type="voice"
        )

        prep_vcs = []
        judgment_vc = None

        if debate_format == 1:
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

        state["room_channel_map"][category.id] = {
            "room_num": room_num,
            "poi": poi_ch.id,
            "timer": timer_ch.id,
            "logs": logs_ch.id,
            "logs_msg": logs_msg.id,
            "debate_vc": debate_vc.id,
            "prep_vcs": [vc.id for vc in prep_vcs],
            "judgment_vc": judgment_vc.id if judgment_vc else None,
            "format": debate_format,
        }

    # ── Abstract Room Lifecycle Factory ───────────────────────────────────────
    async def spawn_room(self, guild: discord.Guild) -> bool:
        """
        Dynamically create a new numbered room at runtime (triggered by dashboard button).
        Derives roles and format from bot.state. Returns True on success.
        """
        state = self.bot.state
        room_map = state.get("room_channel_map", {})
        debate_format = state.get("format", 1)

        existing_nums = [v.get("room_num", 0) for v in room_map.values()]
        next_num = max(existing_nums, default=0) + 1

        roles_ids = state.get("roles", {})
        roles = {}
        for key, rid in roles_ids.items():
            r = guild.get_role(rid)
            if r:
                roles[key] = r

        everyone = guild.default_role
        visitor = roles.get("Visitor")
        org = roles.get("ORG")
        cap = roles.get("CAP")
        tabby = roles.get("Tabby")
        equity = roles.get("Equity Officer")
        invited_adj = roles.get("Invited Adj")
        indep_adj = roles.get("Independent Adj")
        debater = roles.get("Debater")

        high_command = [r for r in [org, cap, tabby, equity] if r]
        participant_roles = [r for r in [org, cap, tabby, equity, invited_adj, indep_adj, debater] if r]

        state.setdefault("room_sessions", {})
        state.setdefault("room_channel_map", {})

        try:
            await self._build_single_room(
                guild=guild,
                room_num=next_num,
                debate_format=debate_format,
                everyone=everyone,
                visitor=visitor,
                high_command=high_command,
                participant_roles=participant_roles,
                state=state,
            )
            return True
        except Exception as e:
            print(f"[RoomEngine] spawn_room failed: {e}")
            return False

    async def remove_room(self, guild: discord.Guild) -> bool:
        """
        Dynamically destroy the highest-numbered active room at runtime.
        Returns True on success, False if no rooms exist.
        """
        state = self.bot.state
        room_map = state.get("room_channel_map", {})

        if not room_map:
            return False

        highest_cat_id = max(
            room_map.keys(),
            key=lambda cid: room_map[cid].get("room_num", 0),
        )
        room_data = room_map[highest_cat_id]

        category = guild.get_channel(highest_cat_id)
        if category and isinstance(category, discord.CategoryChannel):
            for ch in list(category.channels):
                try:
                    await ch.delete(reason="KAMLABot: remove_room dashboard action")
                    await asyncio.sleep(1.2)
                except discord.HTTPException:
                    pass
            try:
                await category.delete(reason="KAMLABot: remove_room dashboard action")
                await asyncio.sleep(1.2)
            except discord.HTTPException:
                pass

        state["room_channel_map"].pop(highest_cat_id, None)
        state["room_sessions"].pop(highest_cat_id, None)
        return True

    # ── Hot-Swap Format Matrix (AP ⇄ BP) ─────────────────────────────────────
    async def switch_format(self, guild: discord.Guild) -> bool:
        """
        Triggered by the 'Switch Format (AP/BP)' dashboard button.

        For every live room:
          1. Identify and DELETE only the format-specific prep VCs.
          2. Inject the new format's prep VCs immediately.

        CRITICAL PRESERVATION: #timer, #poi, #logs, and the primary 'debate'
        voice channel are NEVER touched. They remain fully active throughout.

        Returns True on success.
        """
        state = self.bot.state
        room_map = state.get("room_channel_map", {})
        if not room_map:
            return False

        current_format = state.get("format", 1)
        new_format = 2 if current_format == 1 else 1

        roles_ids = state.get("roles", {})
        roles = {}
        for key, rid in roles_ids.items():
            r = guild.get_role(rid)
            if r:
                roles[key] = r

        everyone = guild.default_role
        visitor = roles.get("Visitor")
        org = roles.get("ORG")
        cap = roles.get("CAP")
        tabby = roles.get("Tabby")
        equity = roles.get("Equity Officer")
        invited_adj = roles.get("Invited Adj")
        indep_adj = roles.get("Independent Adj")
        debater = roles.get("Debater")

        participant_roles = [r for r in [org, cap, tabby, equity, invited_adj, indep_adj, debater] if r]

        cat_ow = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in participant_roles:
            cat_ow[r] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, connect=True, speak=True,
            )
        if visitor:
            cat_ow[visitor] = discord.PermissionOverwrite(view_channel=False)
        text_ow = dict(cat_ow)

        for cat_id, room_data in list(room_map.items()):
            category = guild.get_channel(cat_id)
            if not category or not isinstance(category, discord.CategoryChannel):
                continue

            # ── Step 1: Delete old prep VCs (NEVER touch timer/poi/logs/debate) ──
            old_prep_ids = set(room_data.get("prep_vcs", []))
            old_judgment_id = room_data.get("judgment_vc")

            for prep_id in old_prep_ids:
                ch = guild.get_channel(prep_id)
                if ch:
                    try:
                        await ch.delete(reason="KAMLABot: format switch — removing old prep VCs")
                        await asyncio.sleep(1.2)
                    except discord.HTTPException as e:
                        print(f"[RoomEngine] Failed to delete prep VC {prep_id}: {e}")

            if old_judgment_id:
                jch = guild.get_channel(old_judgment_id)
                if jch:
                    try:
                        await jch.delete(reason="KAMLABot: format switch — removing old judgment VC")
                        await asyncio.sleep(1.2)
                    except discord.HTTPException as e:
                        print(f"[RoomEngine] Failed to delete judgment VC: {e}")

            # ── Step 2: Inject new format prep VCs ───────────────────────────
            new_prep_vcs = []
            new_judgment_vc = None

            if new_format == 1:
                # AP format
                gov_prep = await self._make(
                    guild, "GOV Prep", category, text_ow,
                    channel_type="voice", user_limit=3,
                )
                opp_prep = await self._make(
                    guild, "OPP Prep", category, text_ow,
                    channel_type="voice", user_limit=3,
                )
                new_judgment_vc = await self._make(
                    guild, "Judgment", category, text_ow, channel_type="voice"
                )
                new_prep_vcs = [gov_prep, opp_prep]

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
                new_judgment_vc = await self._make(
                    guild, "Judgment", category, text_ow, channel_type="voice"
                )
                new_prep_vcs = [og_prep, oo_prep, cg_prep, co_prep]

            # ── Step 3: Update state (preserve all other keys) ────────────────
            room_data["prep_vcs"] = [vc.id for vc in new_prep_vcs]
            room_data["judgment_vc"] = new_judgment_vc.id if new_judgment_vc else None
            room_data["format"] = new_format

        state["format"] = new_format
        return True

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

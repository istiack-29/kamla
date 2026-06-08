"""
KAMLABot — room_engine.py
Cog: RoomEngine

Handles algorithm-driven generation of debate room categories.
Each room category contains:
  Text:  timer, poi, logs
  Voice: debate
  Voice (AP): GOV Prep (max 3), OPP Prep (max 3), Judgment
  Voice (BP): OG Prep (max 2), OO Prep (max 2), CG Prep (max 2), CO Prep (max 2), Judgment

[span_1](start_span)Rate-limit evasion: await asyncio.sleep(1.2) between EVERY channel/category creation.[span_1](end_span)

Abstract Room Lifecycle Factory:
  - [span_2](start_span)spawn_room()   — independently create a new numbered room at runtime[span_2](end_span)
  - [span_3](start_span)remove_room()  — destroy the highest-numbered room category at runtime[span_3](end_span)
  - [span_4](start_span)switch_format() — hot-swap all active rooms between AP and BP format[span_4](end_span)

Hot-Swap Format Matrix (AP ⇄ BP):
  CRITICAL — #timer, #poi, #logs, and the primary 'debate' voice channel
  are NEVER touched during conversion. Only format-specific prep VCs are
  [span_5](start_span)deleted and re-injected.[span_5](end_span)

Visitor restriction logic:
  Visitors can ONLY see and join the 'debate' voice channel per room.
  [span_6](start_span)All other room channels are hidden from Visitors.[span_6](end_span)
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
        [span_7](start_span)user_limit: int = 0,[span_7](end_span)
    ) -> discord.abc.GuildChannel:
        """
        Single channel factory with mandatory rate-limit sleep.
        Every call sleeps 1.2 s BEFORE creation to respect Discord's
        [span_8](start_span)rate limit on bulk channel creation (HTTP 429 avoidance).[span_8](end_span)
        """
        [span_9](start_span)await asyncio.sleep(1.2)[span_9](end_span)
        if channel_type == "voice":
            return await guild.create_voice_channel(
                name,
                category=category,
                overwrites=overwrites,
                [span_10](start_span)user_limit=user_limit if user_limit else 0,[span_10](end_span)
            )
        return await guild.create_text_channel(
            name,
            category=category,
            overwrites=overwrites,
        )

    # ── Build all rooms (initial setup) ───────────────────────────────────────
    async def build_rooms(
        self,
        [span_11](start_span)guild: discord.Guild,[span_11](end_span)
        roles: dict,
        debate_format: int,
        num_rooms: int,
    ):
        """
        Main entry point.
        [span_12](start_span)Creates `num_rooms` room categories sequentially.[span_12](end_span)
        [span_13](start_span)Sequential (not parallel) to avoid rate-limit spikes.[span_13](end_span)
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
        [span_14](start_span)debater = roles.get("Debater")[span_14](end_span)

        high_command = [r for r in [org, cap, tabby, equity] if r]
        participant_roles = [r for r in [org, cap, tabby, equity, invited_adj, indep_adj, debater] if r]

        state["room_sessions"] = {}
        state["room_channel_map"] = {}

        for room_num in range(1, num_rooms + 1):
            await self._build_single_room(
                [span_15](start_span)guild=guild,[span_15](end_span)
                room_num=room_num,
                debate_format=debate_format,
                everyone=everyone,
                visitor=visitor,
                high_command=high_command,
                [span_16](start_span)participant_roles=participant_roles,[span_16](end_span)
                state=state,
            )

    # ── Single room builder ───────────────────────────────────────────────────
    async def _build_single_room(
        self,
        guild: discord.Guild,
        room_num: int,
        debate_format: int,
        everyone: discord.Role,
        [span_17](start_span)visitor: discord.Role | None,[span_17](end_span)
        high_command: list,
        participant_roles: list,
        state: dict,
    ):
        """Create one room category and all its channels."""

        # A. Category Level Overwrites (The Gatekeeper)
        cat_ow = {
            [span_18](start_span)everyone: discord.PermissionOverwrite(view_channel=False),[span_18](end_span)
        }
        for r in participant_roles:
            cat_ow[r] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                connect=True,
                speak=True,
            [span_19](start_span))
        
        # FIX: Visitor must see the category to see any channels inside it.
        # But we restrict connect/speak globally at the category level.
        if visitor:
            cat_ow[visitor] = discord.PermissionOverwrite(
                view_channel=True,
                connect=False,
                speak=False
            )

        await asyncio.sleep(1.2)[span_19](end_span)
        category = await guild.create_category(
            f"ROOM {room_num}",
            overwrites=cat_ow,
        )

        # B. Text Channels & Prep/Judgment VCs Overwrites
        text_ow = dict(cat_ow)
        if visitor:
            # FIX: Explicitly hide all text and prep/judgment channels from visitors
            text_ow[visitor] = discord.PermissionOverwrite(view_channel=False)

        # C. Primary 'debate' Voice Channel Overwrites
        debate_ow = dict(cat_ow)
        if visitor:
            # Visitors can see and connect, but remain muted.
            debate_ow[visitor] = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=False,
                use_soundboard=False,
            [span_20](start_span))

        timer_ch = await self._make(guild, "timer", category, text_ow)[span_20](end_span)
        poi_ch = await self._make(guild, "poi", category, text_ow)
        [span_21](start_span)logs_ch = await self._make(guild, "logs", category, text_ow)[span_21](end_span)

        state["room_sessions"][category.id] = {}
        session_embed = discord.Embed(
            title=f"🔊 ROOM {room_num} — Session Log",
            description="No voice activity yet.",
            color=discord.Color.dark_teal(),
        [span_22](start_span))
        view = SeeWhoView(self.bot, category_id=category.id)
        logs_msg = await logs_ch.send(embed=session_embed, view=view)[span_22](end_span)

        debate_vc = await self._make(
            guild, "debate", category, debate_ow, channel_type="voice"
        [span_23](start_span))

        prep_vcs = []
        judgment_vc = None

        if debate_format == 1:
            gov_prep = await self._make(
                guild, "GOV Prep", category, text_ow,
                channel_type="voice", user_limit=3,
            )[span_23](end_span)
            opp_prep = await self._make(
                guild, "OPP Prep", category, text_ow,
                channel_type="voice", user_limit=3,
            [span_24](start_span))
            judgment_vc = await self._make(
                guild, "Judgment", category, text_ow, channel_type="voice"
            )
            prep_vcs = [gov_prep, opp_prep][span_24](end_span)

        else:
            og_prep = await self._make(
                guild, "OG Prep", category, text_ow,
                channel_type="voice", user_limit=2,
            [span_25](start_span))
            oo_prep = await self._make(
                guild, "OO Prep", category, text_ow,
                channel_type="voice", user_limit=2,
            )[span_25](end_span)
            cg_prep = await self._make(
                guild, "CG Prep", category, text_ow,
                channel_type="voice", user_limit=2,
            )
            co_prep = await self._make(
                guild, "CO Prep", category, text_ow,
                channel_type="voice", user_limit=2,
            [span_26](start_span))
            judgment_vc = await self._make(
                guild, "Judgment", category, text_ow, channel_type="voice"
            )
            prep_vcs = [og_prep, oo_prep, cg_prep, co_prep][span_26](end_span)

        state["room_channel_map"][category.id] = {
            "room_num": room_num,
            "poi": poi_ch.id,
            "timer": timer_ch.id,
            "logs": logs_ch.id,
            "logs_msg": logs_msg.id,
            "debate_vc": debate_vc.id,
            [span_27](start_span)"prep_vcs": [vc.id for vc in prep_vcs],[span_27](end_span)
            "judgment_vc": judgment_vc.id if judgment_vc else None,
            "format": debate_format,
        [span_28](start_span)}

    # ── Abstract Room Lifecycle Factory ───────────────────────────────────────
    async def spawn_room(self, guild: discord.Guild) -> bool:
        """
        Dynamically create a new numbered room at runtime (triggered by dashboard button).[span_28](end_span)
        Derives roles and format from bot.state. [span_29](start_span)Returns True on success.[span_29](end_span)
        """
        state = self.bot.state
        room_map = state.get("room_channel_map", {})
        [span_30](start_span)debate_format = state.get("format", 1)[span_30](end_span)

        existing_nums = [v.get("room_num", 0) for v in room_map.values()]
        [span_31](start_span)next_num = max(existing_nums, default=0) + 1[span_31](end_span)

        roles_ids = state.get("roles", {})
        roles = {}
        for key, rid in roles_ids.items():
            r = guild.get_role(rid)
            if r:
                [span_32](start_span)roles[key] = r[span_32](end_span)

        everyone = guild.default_role
        visitor = roles.get("Visitor")
        org = roles.get("ORG")
        cap = roles.get("CAP")
        tabby = roles.get("Tabby")
        equity = roles.get("Equity Officer")
        [span_33](start_span)invited_adj = roles.get("Invited Adj")[span_33](end_span)
        indep_adj = roles.get("Independent Adj")
        [span_34](start_span)debater = roles.get("Debater")[span_34](end_span)

        high_command = [r for r in [org, cap, tabby, equity] if r]
        [span_35](start_span)participant_roles = [r for r in [org, cap, tabby, equity, invited_adj, indep_adj, debater] if r][span_35](end_span)

        state.setdefault("room_sessions", {})
        [span_36](start_span)state.setdefault("room_channel_map", {})[span_36](end_span)

        try:
            await self._build_single_room(
                guild=guild,
                room_num=next_num,
                debate_format=debate_format,
                everyone=everyone,
                visitor=visitor,
                [span_37](start_span)high_command=high_command,[span_37](end_span)
                participant_roles=participant_roles,
                state=state,
            )
            return True
        except Exception as e:
            print(f"[RoomEngine] spawn_room failed: {e}")
            [span_38](start_span)return False[span_38](end_span)

    async def remove_room(self, guild: discord.Guild) -> bool:
        """
        Dynamically destroy the highest-numbered active room at runtime.
        [span_39](start_span)Returns True on success, False if no rooms exist.[span_39](end_span)
        """
        state = self.bot.state
        [span_40](start_span)room_map = state.get("room_channel_map", {})[span_40](end_span)

        if not room_map:
            return False

        highest_cat_id = max(
            room_map.keys(),
            key=lambda cid: room_map[cid].get("room_num", 0),
        )
        [span_41](start_span)room_data = room_map[highest_cat_id][span_41](end_span)

        category = guild.get_channel(highest_cat_id)
        if category and isinstance(category, discord.CategoryChannel):
            for ch in list(category.channels):
                try:
                    await ch.delete(reason="KAMLABot: remove_room dashboard action")
                    [span_42](start_span)await asyncio.sleep(1.2)[span_42](end_span)
                except discord.HTTPException:
                    pass
            try:
                await category.delete(reason="KAMLABot: remove_room dashboard action")
                await asyncio.sleep(1.2)
            except discord.HTTPException:
                [span_43](start_span)pass[span_43](end_span)

        state["room_channel_map"].pop(highest_cat_id, None)
        state["room_sessions"].pop(highest_cat_id, None)
        [span_44](start_span)return True[span_44](end_span)

    # ── Hot-Swap Format Matrix (AP ⇄ BP) ─────────────────────────────────────
    async def switch_format(self, guild: discord.Guild) -> bool:
        """
        Triggered by the 'Switch Format (AP/BP)' dashboard button.
        For every live room:
          1. Identify and DELETE only the format-specific prep VCs.
          2. [span_45](start_span)Inject the new format's prep VCs immediately.[span_45](end_span)

        CRITICAL PRESERVATION: #timer, #poi, #logs, and the primary 'debate'
        voice channel are NEVER touched.
        [span_46](start_span)They remain fully active throughout.[span_46](end_span)

        Returns True on success.
        """
        state = self.bot.state
        room_map = state.get("room_channel_map", {})
        if not room_map:
            [span_47](start_span)return False[span_47](end_span)

        current_format = state.get("format", 1)
        [span_48](start_span)new_format = 2 if current_format == 1 else 1[span_48](end_span)

        roles_ids = state.get("roles", {})
        [span_49](start_span)roles = {}[span_49](end_span)
        for key, rid in roles_ids.items():
            r = guild.get_role(rid)
            if r:
                [span_50](start_span)roles[key] = r[span_50](end_span)

        everyone = guild.default_role
        visitor = roles.get("Visitor")
        org = roles.get("ORG")
        cap = roles.get("CAP")
        [span_51](start_span)tabby = roles.get("Tabby")[span_51](end_span)
        equity = roles.get("Equity Officer")
        invited_adj = roles.get("Invited Adj")
        indep_adj = roles.get("Independent Adj")
        [span_52](start_span)debater = roles.get("Debater")[span_52](end_span)

        [span_53](start_span)participant_roles = [r for r in [org, cap, tabby, equity, invited_adj, indep_adj, debater] if r][span_53](end_span)

        # FIX: Re-apply correct gatekeeping permissions during format switch
        cat_ow = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in participant_roles:
            cat_ow[r] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, connect=True, speak=True,
            [span_54](start_span))
            
        if visitor:
            cat_ow[visitor] = discord.PermissionOverwrite(
                view_channel=True, connect=False, speak=False
            )
            
        text_ow = dict(cat_ow)
        if visitor:
            text_ow[visitor] = discord.PermissionOverwrite(view_channel=False)

        for cat_id, room_data in list(room_map.items()):[span_54](end_span)
            category = guild.get_channel(cat_id)
            if not category or not isinstance(category, discord.CategoryChannel):
                [span_55](start_span)continue[span_55](end_span)

            # ── Step 1: Delete old prep VCs (NEVER touch timer/poi/logs/debate) ──
            old_prep_ids = set(room_data.get("prep_vcs", []))
            [span_56](start_span)old_judgment_id = room_data.get("judgment_vc")[span_56](end_span)

            for prep_id in old_prep_ids:
                ch = guild.get_channel(prep_id)
                if ch:
                    try:
                        [span_57](start_span)await ch.delete(reason="KAMLABot: format switch — removing old prep VCs")[span_57](end_span)
                        await asyncio.sleep(1.2)
                    except discord.HTTPException as e:
                        [span_58](start_span)print(f"[RoomEngine] Failed to delete prep VC {prep_id}: {e}")[span_58](end_span)

            if old_judgment_id:
                [span_59](start_span)jch = guild.get_channel(old_judgment_id)[span_59](end_span)
                if jch:
                    try:
                        await jch.delete(reason="KAMLABot: format switch — removing old judgment VC")
                        [span_60](start_span)await asyncio.sleep(1.2)[span_60](end_span)
                    except discord.HTTPException as e:
                        [span_61](start_span)print(f"[RoomEngine] Failed to delete judgment VC: {e}")[span_61](end_span)

            # ── Step 2: Inject new format prep VCs ───────────────────────────
            new_prep_vcs = []
            [span_62](start_span)new_judgment_vc = None[span_62](end_span)

            if new_format == 1:
                # AP format
                gov_prep = await self._make(
                    guild, "GOV Prep", category, text_ow,
                    [span_63](start_span)channel_type="voice", user_limit=3,[span_63](end_span)
                )
                opp_prep = await self._make(
                    guild, "OPP Prep", category, text_ow,
                    channel_type="voice", user_limit=3,
                [span_64](start_span))
                new_judgment_vc = await self._make(
                    guild, "Judgment", category, text_ow, channel_type="voice"
                )
                new_prep_vcs = [gov_prep, opp_prep][span_64](end_span)

            else:
                # BP format
                og_prep = await self._make(
                    guild, "OG Prep", category, text_ow,
                    channel_type="voice", user_limit=2,
                [span_65](start_span))
                oo_prep = await self._make(
                    guild, "OO Prep", category, text_ow,
                    channel_type="voice", user_limit=2,
                )[span_65](end_span)
                cg_prep = await self._make(
                    guild, "CG Prep", category, text_ow,
                    channel_type="voice", user_limit=2,
                [span_66](start_span))
                co_prep = await self._make(
                    guild, "CO Prep", category, text_ow,
                    channel_type="voice", user_limit=2,
                )[span_66](end_span)
                new_judgment_vc = await self._make(
                    guild, "Judgment", category, text_ow, channel_type="voice"
                )
                [span_67](start_span)new_prep_vcs = [og_prep, oo_prep, cg_prep, co_prep][span_67](end_span)

            # ── Step 3: Update state (preserve all other keys) ────────────────
            room_data["prep_vcs"] = [vc.id for vc in new_prep_vcs]
            room_data["judgment_vc"] = new_judgment_vc.id if new_judgment_vc else None
            [span_68](start_span)room_data["format"] = new_format[span_68](end_span)

        state["format"] = new_format
        [span_69](start_span)return True[span_69](end_span)

    # ── Room voice session tracking ───────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        state = self.bot.state
        [span_70](start_span)room_map = state.get("room_channel_map", {})[span_70](end_span)

        [span_71](start_span)def find_room(channel: discord.VoiceChannel | None):[span_71](end_span)
            if not channel:
                return None
            for cat_id, data in room_map.items():
                all_vcs = [data["debate_vc"]] + data["prep_vcs"]
                if data.get("judgment_vc"):
                    [span_72](start_span)all_vcs.append(data["judgment_vc"])[span_72](end_span)
                if channel.id in all_vcs:
                    return cat_id
            [span_73](start_span)return None[span_73](end_span)

        joined_room = find_room(after.channel)
        [span_74](start_span)left_room = find_room(before.channel)[span_74](end_span)

        if joined_room and joined_room != left_room:
            [span_75](start_span)state["room_sessions"].setdefault(joined_room, {})[span_75](end_span)
            state["room_sessions"][joined_room][member.id] = {
                "join": discord.utils.utcnow(),
                "exit": None,
            }
            [span_76](start_span)await self._refresh_room_log(member.guild, joined_room)[span_76](end_span)

        if left_room and left_room != joined_room:
            [span_77](start_span)sessions = state["room_sessions"].get(left_room, {})[span_77](end_span)
            if member.id in sessions and sessions[member.id]["exit"] is None:
                sessions[member.id]["exit"] = discord.utils.utcnow()
            [span_78](start_span)await self._refresh_room_log(member.guild, left_room)[span_78](end_span)

    async def _refresh_room_log(self, guild: discord.Guild, category_id: int):
        """Update the room's logs channel embed."""
        state = self.bot.state
        [span_79](start_span)room_data = state.get("room_channel_map", {}).get(category_id)[span_79](end_span)
        if not room_data:
            return

        logs_ch = guild.get_channel(room_data["logs"])
        if not logs_ch:
            [span_80](start_span)return[span_80](end_span)

        try:
            logs_msg = await logs_ch.fetch_message(room_data["logs_msg"])
        except discord.NotFound:
            [span_81](start_span)return[span_81](end_span)

        [span_82](start_span)sessions = state["room_sessions"].get(category_id, {})[span_82](end_span)
        active = sum(1 for s in sessions.values() if s["exit"] is None)

        category = guild.get_channel(category_id)
        [span_83](start_span)room_name = category.name if category else f"Room {category_id}"[span_83](end_span)

        embed = discord.Embed(
            title=f"🔊 {room_name} — Session Log",
            description=(
                [span_84](start_span)f"**Currently active:** {active} participant(s)\n"[span_84](end_span)
                f"**Total tracked:** {len(sessions)}\n\n"
                "Press **👥 See Who** for detailed entry/exit/duration data."
            ),
            color=discord.Color.dark_teal(),
            timestamp=discord.utils.utcnow(),
        )
        [span_85](start_span)view = SeeWhoView(self.bot, category_id=category_id)[span_85](end_span)
        [span_86](start_span)await logs_msg.edit(embed=embed, view=view)[span_86](end_span)


async def setup(bot: commands.Bot):
    [span_87](start_span)await bot.add_cog(RoomEngine(bot))[span_87](end_span)

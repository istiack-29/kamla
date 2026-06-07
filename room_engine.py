"""
KAMLABot — room_engine.py
Cog: RoomEngine

Dynamic factory for debate room categories.

Each room category contains:
    Text  : timer, poi, logs
    Voice : debate              (visible to Visitors)
    Voice : <format-specific prep rooms>, Judgment

Formats:
    AP (1):  GOV Prep (limit 3), OPP Prep (limit 3), Judgment
    BP (2):  OG Prep / OO Prep / CG Prep / CO Prep (limit 2 each), Judgment

Rate-limit evasion:
    Every guild mutation sleeps RATE_SLEEP seconds BEFORE the call.

Hot-swap:
    swap_format() destroys ONLY the prep voice channels of each room and
    rebuilds them in the new format. The text channels (timer/poi/logs)
    and the `debate` VC are NEVER touched.
"""

import asyncio
import discord
from discord.ext import commands


RATE_SLEEP = 1.2  # mandatory safety buffer for every channel mutation

# format constants
AP = 1
BP = 2

PREP_AP = [("🎙️ GOV Prep", 3), ("🎙️ OPP Prep", 3)]
PREP_BP = [
    ("🎙️ OG Prep", 2),
    ("🎙️ OO Prep", 2),
    ("🎙️ CG Prep", 2),
    ("🎙️ CO Prep", 2),
]


def _prep_specs(fmt: int) -> list[tuple[str, int]]:
    return list(PREP_BP) if fmt == BP else list(PREP_AP)


def _allow(*roles):
    ow = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, read_message_history=True, connect=True, speak=True
    )
    return {r: ow for r in roles if r}


def _deny_view(role):
    return {role: discord.PermissionOverwrite(view_channel=False)} if role else {}


class RoomEngine(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── primitive factory ────────────────────────────────────────────────────
    async def _sleep(self):
        await asyncio.sleep(RATE_SLEEP)

    async def _mk_text(self, guild, name, category, overwrites):
        await self._sleep()
        return await guild.create_text_channel(name, category=category, overwrites=overwrites)

    async def _mk_voice(self, guild, name, category, overwrites, user_limit=0):
        await self._sleep()
        return await guild.create_voice_channel(
            name, category=category, overwrites=overwrites, user_limit=user_limit or 0
        )

    # ── overwrite recipes ────────────────────────────────────────────────────
    def _room_overwrites(self, guild, roles: dict, *, allow_visitors: bool):
        everyone = guild.default_role
        visitor = roles.get("Visitor")
        org = roles.get("ORG")
        cap = roles.get("CAP")
        tabby = roles.get("Tabby")
        equity = roles.get("Equity Officer")
        invited = roles.get("Invited Adj")
        indep = roles.get("Independent Adj")
        debater = roles.get("Debater")

        ow = {everyone: discord.PermissionOverwrite(view_channel=False)}
        # high command + adj + debater can see
        for r in (org, cap, tabby, equity, invited, indep, debater):
            if r:
                ow[r] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    connect=True, speak=True,
                )
        # visitors get debate only (allow_visitors=True), else hidden
        if visitor:
            ow[visitor] = discord.PermissionOverwrite(view_channel=allow_visitors, connect=allow_visitors)
        return ow

    # ── build N rooms ────────────────────────────────────────────────────────
    async def build_rooms(self, guild, roles: dict, debate_format: int, num_rooms: int):
        state = self.bot.state
        room_map: dict = state.setdefault("room_channel_map", {})

        for i in range(1, num_rooms + 1):
            await self._build_single_room(guild, roles, debate_format, i, room_map)

    async def _build_single_room(self, guild, roles, debate_format, index, room_map):
        name = f"🏟️ Room {index}"
        await self._sleep()
        category = await guild.create_category(
            name, overwrites=self._room_overwrites(guild, roles, allow_visitors=False)
        )

        # text channels (hidden from visitors)
        text_ow = self._room_overwrites(guild, roles, allow_visitors=False)
        timer = await self._mk_text(guild, "⏱️︱timer", category, text_ow)
        poi = await self._mk_text(guild, "🙋︱poi", category, text_ow)
        logs = await self._mk_text(guild, "📜︱logs", category, text_ow)

        # debate VC (visitors allowed)
        debate_ow = self._room_overwrites(guild, roles, allow_visitors=True)
        debate = await self._mk_voice(guild, "🎤 Debate", category, debate_ow)

        # prep + judgment (hidden from visitors)
        prep_ow = self._room_overwrites(guild, roles, allow_visitors=False)
        prep_ids = []
        for label, limit in _prep_specs(debate_format):
            ch = await self._mk_voice(guild, label, category, prep_ow, user_limit=limit)
            prep_ids.append(ch.id)
        judgment = await self._mk_voice(guild, "⚖️ Judgment", category, prep_ow)

        room_map[category.id] = {
            "index": index,
            "category": category.id,
            "timer": timer.id,
            "poi": poi.id,
            "logs": logs.id,
            "debate": debate.id,
            "prep": prep_ids,
            "judgment": judgment.id,
            "format": debate_format,
        }

    # ── add a room dynamically (used by Settings dashboard) ──────────────────
    async def add_room(self, guild) -> int:
        state = self.bot.state
        roles = {k: guild.get_role(rid) for k, rid in state["roles"].items()}
        room_map = state.setdefault("room_channel_map", {})
        next_index = (max((d["index"] for d in room_map.values()), default=0)) + 1
        await self._build_single_room(guild, roles, state.get("format") or AP, next_index, room_map)
        state["num_rooms"] = len(room_map)
        return next_index

    # ── remove the highest-numbered room ─────────────────────────────────────
    async def remove_room(self, guild) -> int | None:
        state = self.bot.state
        room_map: dict = state.setdefault("room_channel_map", {})
        if not room_map:
            return None
        top_cat_id = max(room_map, key=lambda cid: room_map[cid]["index"])
        data = room_map.pop(top_cat_id)

        category = guild.get_channel(top_cat_id)
        if category:
            for ch in list(category.channels):
                try:
                    await self._sleep()
                    await ch.delete(reason="KAMLABot remove_room")
                except discord.HTTPException:
                    pass
            try:
                await self._sleep()
                await category.delete(reason="KAMLABot remove_room")
            except discord.HTTPException:
                pass
        state["num_rooms"] = len(room_map)
        return data["index"]

    # ── hot-swap format AP ⇄ BP for every room ───────────────────────────────
    async def swap_format(self, guild, new_format: int):
        state = self.bot.state
        roles = {k: guild.get_role(rid) for k, rid in state["roles"].items()}
        room_map: dict = state.setdefault("room_channel_map", {})

        for cat_id, data in list(room_map.items()):
            category = guild.get_channel(cat_id)
            if not category:
                continue

            # delete only the prep VCs (NOT timer/poi/logs/debate/judgment)
            for prep_id in list(data.get("prep", [])):
                ch = guild.get_channel(prep_id)
                if ch:
                    try:
                        await self._sleep()
                        await ch.delete(reason="KAMLABot format swap")
                    except discord.HTTPException:
                        pass

            # rebuild prep VCs in new format
            prep_ow = self._room_overwrites(guild, roles, allow_visitors=False)
            new_prep_ids = []
            for label, limit in _prep_specs(new_format):
                ch = await self._mk_voice(guild, label, category, prep_ow, user_limit=limit)
                new_prep_ids.append(ch.id)

            data["prep"] = new_prep_ids
            data["format"] = new_format

        state["format"] = new_format


async def setup(bot: commands.Bot):
    await bot.add_cog(RoomEngine(bot))

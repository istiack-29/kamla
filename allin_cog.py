"""allin_cog.py — "All-In": pull everyone from the prep rooms back to the DEBATE ROOM of the room category.

There is no dedicated all-in channel any more. An eligible member types `.allin` in a room's timer channel (or a
legacy poi channel) and :func:`push_back_all` — the one and only implementation, unchanged in behaviour — moves
the prep-room members back. The `Push Back All` button of older panels still works and calls the same function.
"""

import asyncio
import logging
from dataclasses import dataclass

import discord
from discord.ext import commands

from naming import is_kind
from ratelimit import notify

log = logging.getLogger("kamla.allin")

# Roles that may NOT use All-In. Anybody else who holds at least one role may (ORG, CAP, TABBY, EQUITY, INVITED
# ADJUDICATOR and any other role the server has added). The server owner / an Administrator keeps the access the
# feature always had, unless they also hold one of these roles.
EXCLUDED_ROLES = frozenset({"VISITOR", "INDEPENDENT ADJUDICATOR", "DEBATER"})

PREP_KEYWORDS = {"PREP"}
DEBATE_ROOM_KEYWORD = "DEBATE ROOM"
COOLDOWN_SECONDS = 3.0

_running: set[int] = set()          # category ids with a push-back in progress
_last_run: dict[int, float] = {}


def can_use_allin(member) -> bool:
    """Role-based rule (see EXCLUDED_ROLES)."""
    roles = [r for r in getattr(member, "roles", []) if getattr(r, "name", "") != "@everyone"]
    if any(r.name.upper() in EXCLUDED_ROLES for r in roles):
        return False
    if roles:
        return True
    guild = getattr(member, "guild", None)
    if guild is not None and getattr(guild, "owner_id", None) == getattr(member, "id", None):
        return True
    return bool(getattr(getattr(member, "guild_permissions", None), "administrator", False))


def _is_allowed(interaction: discord.Interaction) -> bool:
    """Button path (older panels): same rule, with Discord's own fresh permission answer for administrators."""
    if getattr(interaction.permissions, "administrator", False) and not any(
            r.name.upper() in EXCLUDED_ROLES for r in getattr(interaction.user, "roles", [])):
        return True
    return can_use_allin(interaction.user)


def _find_debate_room(category: discord.CategoryChannel) -> discord.VoiceChannel | None:
    for ch in category.voice_channels:
        if DEBATE_ROOM_KEYWORD in ch.name.upper():
            return ch
    return None


def _find_prep_channels(category: discord.CategoryChannel) -> list[discord.VoiceChannel]:
    return [ch for ch in category.voice_channels if any(kw in ch.name.upper() for kw in PREP_KEYWORDS)]


@dataclass
class PushResult:
    moved: int = 0
    failed: int = 0
    error: str = ""             # set when nothing could be attempted
    debate_room: object = None


async def push_back_all(category: discord.CategoryChannel | None) -> PushResult:
    """Move every member of the category's PREP voice channels to its DEBATE ROOM."""
    if category is None:
        return PushResult(error="❌ Could not determine the room category.")
    debate_room = _find_debate_room(category)
    if debate_room is None:
        return PushResult(error="❌ Could not find the **DEBATE ROOM** voice channel in this room.")
    prep_channels = _find_prep_channels(category)
    if not prep_channels:
        return PushResult(error="ℹ️ No prep rooms found in this room category.", debate_room=debate_room)
    result = PushResult(debate_room=debate_room)
    for prep_vc in prep_channels:
        for member in list(prep_vc.members):
            ok = await notify.best_effort(lambda m=member: m.move_to(debate_room, reason="KAMLA Push Back All"),
                                          label="allin-move")
            if ok:
                result.moved += 1
            else:
                result.failed += 1
    return result


def describe_result(result: PushResult) -> str:
    if result.error:
        return result.error
    parts = [f"✅ Moved **{result.moved}** member(s) back to **{result.debate_room.name}**."]
    if result.failed:
        parts.append(f"⚠️ Failed to move {result.failed} member(s) (no permission or not in voice).")
    return "\n".join(parts)


def _is_allin_channel(channel) -> bool:
    """Timer channels of any room category, plus the legacy poi channel that older servers may still have."""
    return isinstance(channel, discord.TextChannel) and (is_kind(channel.name, "timer") or is_kind(channel.name, "poi"))


async def handle_allin_message(message: discord.Message) -> None:
    """`.allin` typed in a timer/poi channel. The user's message is left alone (it is never deleted)."""
    channel = message.channel
    if not _is_allin_channel(channel):
        return
    if not can_use_allin(message.author):
        await _reply(channel, f"⛔ {message.author.mention} your role cannot use **All-In**.")
        return
    category = channel.category
    if category is None:
        await _reply(channel, "❌ This channel is not inside a room category.")
        return
    loop = asyncio.get_running_loop()
    now = loop.time()
    if category.id in _running or now - _last_run.get(category.id, -1e9) < COOLDOWN_SECONDS:
        return                                                   # a double-typed `.allin` must not run twice
    _running.add(category.id)
    try:
        result = await push_back_all(category)
        _last_run[category.id] = loop.time()
    finally:
        _running.discard(category.id)
    await _reply(channel, describe_result(result))


async def _reply(channel, text: str) -> None:
    try:
        await channel.send(text, delete_after=15, allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:  # noqa: BLE001 - best effort
        log.debug("Could not answer .allin: %s", e)


class AllInView(discord.ui.View):
    """Kept so the `Push Back All` button of panels that already exist keeps working; same function as `.allin`."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Push Back All", style=discord.ButtonStyle.danger,
                       custom_id="kamla:allin:push_back", emoji="🔴")
    async def push_back_all_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_allowed(interaction):
            await interaction.response.send_message("⛔ Your role cannot use **All-In**.", ephemeral=True)
            return
        channel = interaction.channel
        category = channel.category if isinstance(channel, discord.TextChannel) else None
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(describe_result(await push_back_all(category)), ephemeral=True)


class AllInCog(commands.Cog):
    """No listeners of its own: `.allin` is dispatched by the timer cog (one parser, no conflicts)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(AllInCog(bot))

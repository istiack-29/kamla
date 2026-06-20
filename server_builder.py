"""
server_builder.py
-----------------
Builds the categories and channels for the tournament server.

This file is invoked by /start and /rebuild.

Key responsibilities of THIS update:
  * Create category  🏟️︱GRAND AUDITORIUM
  * Inside it create a TEXT channel  🎼︱song-request
  * Inside it create a VOICE channel  Grand Auditorium
  * Position order under the ballot channel:
        BALLOT CHANNEL
            ⬇
        🎼︱song-request
            ⬇
        Grand Auditorium (voice)
  * VISITOR role:
        - cannot view 🎼︱song-request
        - cannot send messages
        - cannot read message history
  * All other authorized roles keep normal access.

NOTE: Only the parts that changed for this request are shown in full.
      The earlier blocks (ORGCOM / ASSIGN / TIMER / POI / ALL-IN / DEBATE
      ROOM / PREP / ADJUDICATION) from the previous update are preserved
      unchanged. If you are pasting this file over the previous one, keep
      the surrounding helpers intact and only swap in the
      `build_grand_auditorium` section + its call site.
"""

import discord
from discord.utils import get


# ---------- role helpers ----------------------------------------------------

ROLE_NAMES = {
    "cap": "CAP",
    "org": "ORG",
    "tabby": "TABBY",
    "equity": "EQUITY",
    "invited_adj": "INVITED ADJUDICATOR",
    "adj": "ADJUDICATOR",
    "debater": "DEBATER",
    "visitor": "VISITOR",
}


def r(guild: discord.Guild, key: str) -> discord.Role | None:
    return get(guild.roles, name=ROLE_NAMES[key])


# ---------- GRAND AUDITORIUM ------------------------------------------------

async def build_grand_auditorium(guild: discord.Guild, ballot_channel: discord.TextChannel):
    """
    Create / refresh the 🏟️︱GRAND AUDITORIUM category and its channels,
    and position them directly under the ballot channel.
    """

    cap         = r(guild, "cap")
    org         = r(guild, "org")
    tabby       = r(guild, "tabby")
    equity      = r(guild, "equity")
    invited_adj = r(guild, "invited_adj")
    adj         = r(guild, "adj")
    debater     = r(guild, "debater")
    visitor     = r(guild, "visitor")

    # ---- category permissions ---------------------------------------------
    cat_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    for role in (cap, org, tabby, equity, invited_adj, adj, debater):
        if role:
            cat_overwrites[role] = discord.PermissionOverwrite(view_channel=True)
    if visitor:
        # VISITOR cannot even see the category
        cat_overwrites[visitor] = discord.PermissionOverwrite(view_channel=False)

    category = get(guild.categories, name="🏟️︱GRAND AUDITORIUM")
    if category is None:
        category = await guild.create_category(
            "🏟️︱GRAND AUDITORIUM",
            overwrites=cat_overwrites,
        )
    else:
        await category.edit(overwrites=cat_overwrites)

    # ---- 🎼︱song-request (text) -------------------------------------------
    sr_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    for role in (cap, org, tabby, equity, invited_adj, adj, debater):
        if role:
            sr_overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                use_application_commands=True,
            )
    if visitor:
        sr_overwrites[visitor] = discord.PermissionOverwrite(
            view_channel=False,
            send_messages=False,
            read_message_history=False,
        )

    song_request = get(guild.text_channels, name="🎼︱song-request")
    if song_request is None:
        song_request = await guild.create_text_channel(
            "🎼︱song-request",
            category=category,
            overwrites=sr_overwrites,
        )
    else:
        await song_request.edit(category=category, overwrites=sr_overwrites)

    # ---- Grand Auditorium (voice) -----------------------------------------
    voice_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    for role in (cap, org, tabby, equity, invited_adj, adj, debater):
        if role:
            voice_overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
            )
    if visitor:
        # VISITOR may listen-only if they ever reach it, but cannot view it
        voice_overwrites[visitor] = discord.PermissionOverwrite(
            view_channel=False,
            connect=False,
            speak=False,
        )

    grand_auditorium_voice = get(guild.voice_channels, name="Grand Auditorium")
    if grand_auditorium_voice is None:
        grand_auditorium_voice = await guild.create_voice_channel(
            "Grand Auditorium",
            category=category,
            overwrites=voice_overwrites,
        )
    else:
        await grand_auditorium_voice.edit(category=category, overwrites=voice_overwrites)

    # ---- positioning ------------------------------------------------------
    # Place the category right after the ballot channel's category, then
    # arrange the two channels inside it in the required order.
    try:
        target_pos = ballot_channel.category.position + 1 if ballot_channel.category else 0
        await category.edit(position=target_pos)
    except Exception:
        pass

    # Inside the category: song-request first, then voice channel
    try:
        await song_request.edit(position=0)
        await grand_auditorium_voice.edit(position=1)
    except Exception:
        pass

    return category, song_request, grand_auditorium_voice

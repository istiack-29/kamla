"""
music_cog.py
------------
Handles /play, paginated search, the permanent player embed, and
playback controls for the GRAND AUDITORIUM voice channel.

Rules implemented here:

1.  /play <query>
        - Searches YouTube via yt-dlp
        - Returns PAGINATED results, NOT a fixed top-5.
        - Page 1:  results 1-4 + [▶ Next]
        - Page 2:  [◀ Prev] + results 5-7 + [▶ Next]
        - Page 3:  [◀ Prev] + results 8-10 + [▶ Next]
        - ... continues until results are exhausted.
        - Number-emoji buttons (1️⃣..🔟) select that result.
        - Only the command invoker may navigate / select / cancel.
          Anyone else gets an ephemeral:
            "❌ Only the requester can interact with this search."

2.  Permanent player embed
        - One persistent message per guild.
        - Edited in place every time the queue or current track changes.
        - Never re-sent as a new message.

3.  Playback controls (⏯ ⏭ ⏹ 🔁 🔀)
        - Allowed only for: current song requester, ORG, CAP.
        - Anyone else gets an ephemeral:
            "❌ You do not have permission to control playback."

4.  Voice restriction
        - The bot only ever connects to the voice channel named
          "Grand Auditorium".  Any other voice channel is refused.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

# yt-dlp is used for search + stream URL extraction
import yt_dlp  # type: ignore


NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
                 "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

ALLOWED_VOICE_CHANNEL_NAME = "Grand Auditorium"
SONG_REQUEST_CHANNEL_NAME  = "🎼︱song-request"


# ---------- data models -----------------------------------------------------

@dataclass
class Track:
    title: str
    url: str           # stream URL or webpage URL
    webpage_url: str
    duration: int
    requester_id: int
    requester_mention: str


@dataclass
class GuildMusicState:
    queue: list[Track] = field(default_factory=list)
    current: Optional[Track] = None
    player_message_id: Optional[int] = None
    player_channel_id: Optional[int] = None
    loop: bool = False
    voice_client: Optional[discord.VoiceClient] = None


# ---------- yt-dlp helper ---------------------------------------------------

YTDL_SEARCH_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
    "default_search": "ytsearch25",   # up to 25 results so pagination is meaningful
    "noplaylist": True,
}

YTDL_STREAM_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
}


def _search_sync(query: str) -> list[dict]:
    with yt_dlp.YoutubeDL(YTDL_SEARCH_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
    entries = info.get("entries") or []
    return [e for e in entries if e]


def _resolve_stream_sync(webpage_url: str) -> dict:
    with yt_dlp.YoutubeDL(YTDL_STREAM_OPTS) as ydl:
        return ydl.extract_info(webpage_url, download=False)


# ---------- pagination view -------------------------------------------------

PAGE_SIZES = [4, 3, 3]   # Page1=4, Page2=3, Page3=3, then 3 per page after


def _page_slice(results: list[dict], page: int) -> tuple[int, int]:
    """Return (start_index, end_index_exclusive) for `page` (0-indexed)."""
    start = 0
    for i in range(page):
        size = PAGE_SIZES[i] if i < len(PAGE_SIZES) else 3
        start += size
    size = PAGE_SIZES[page] if page < len(PAGE_SIZES) else 3
    return start, min(start + size, len(results))


def _total_pages(n_results: int) -> int:
    used, pages = 0, 0
    while used < n_results:
        size = PAGE_SIZES[pages] if pages < len(PAGE_SIZES) else 3
        used += size
        pages += 1
    return pages


class SearchView(discord.ui.View):
    def __init__(self, cog: "MusicCog", requester: discord.Member,
                 results: list[dict], page: int = 0):
        super().__init__(timeout=120)
        self.cog = cog
        self.requester = requester
        self.results = results
        self.page = page
        self._build()

    # ---- interaction gate ------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                "❌ Only the requester can interact with this search.",
                ephemeral=True,
            )
            return False
        return True

    # ---- rebuild buttons for current page -------------------------------
    def _build(self):
        self.clear_items()
        total_pages = _total_pages(len(self.results))
        start, end = _page_slice(self.results, self.page)

        # ◀ Previous
        if self.page > 0:
            prev_btn = discord.ui.Button(style=discord.ButtonStyle.secondary,
                                         emoji="◀", custom_id="prev")
            prev_btn.callback = self._on_prev
            self.add_item(prev_btn)

        # Number buttons for this page's results
        for local_i, abs_i in enumerate(range(start, end)):
            emoji = NUMBER_EMOJIS[abs_i] if abs_i < len(NUMBER_EMOJIS) else None
            label = None if emoji else str(abs_i + 1)
            btn = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                emoji=emoji,
                label=label,
                custom_id=f"pick:{abs_i}",
            )
            btn.callback = self._make_pick(abs_i)
            self.add_item(btn)

        # ▶ Next
        if self.page < total_pages - 1:
            next_btn = discord.ui.Button(style=discord.ButtonStyle.secondary,
                                         emoji="▶", custom_id="next")
            next_btn.callback = self._on_next
            self.add_item(next_btn)

        # ✖ Cancel
        cancel_btn = discord.ui.Button(style=discord.ButtonStyle.danger,
                                       label="Cancel", custom_id="cancel")
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    # ---- embed for current page -----------------------------------------
    def render_embed(self) -> discord.Embed:
        start, end = _page_slice(self.results, self.page)
        total_pages = _total_pages(len(self.results))

        emb = discord.Embed(
            title=f"🔎 Search Results  •  Page {self.page + 1}/{total_pages}",
            color=discord.Color.blurple(),
        )
        lines = []
        for i in range(start, end):
            r = self.results[i]
            emoji = NUMBER_EMOJIS[i] if i < len(NUMBER_EMOJIS) else f"{i+1}."
            title = r.get("title") or "Unknown"
            dur = r.get("duration")
            dur_s = f" `[{_fmt_duration(dur)}]`" if dur else ""
            lines.append(f"{emoji}  **{title}**{dur_s}")
        emb.description = "\n".join(lines) if lines else "_No results._"
        emb.set_footer(text=f"Requested by {self.requester.display_name}")
        return emb

    # ---- callbacks -------------------------------------------------------
    async def _on_prev(self, interaction: discord.Interaction):
        self.page -= 1
        self._build()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction):
        self.page += 1
        self._build()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    async def _on_cancel(self, interaction: discord.Interaction):
        self.stop()
        try:
            await interaction.message.delete()
        except Exception:
            await interaction.response.edit_message(content="❌ Cancelled.",
                                                    embed=None, view=None)

    def _make_pick(self, index: int):
        async def _cb(interaction: discord.Interaction):
            await interaction.response.defer()
            await self.cog.handle_pick(interaction, self.requester,
                                       self.results[index], self_message=interaction.message)
            self.stop()
        return _cb


def _fmt_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "?:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------- player control view --------------------------------------------

class PlayerControls(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    async def _gate(self, interaction: discord.Interaction) -> bool:
        state = self.cog.get_state(self.guild_id)
        member: discord.Member = interaction.user  # type: ignore
        role_names = {r.name for r in member.roles}
        is_requester = state.current and state.current.requester_id == member.id
        if is_requester or {"ORG", "CAP"} & role_names:
            return True
        await interaction.response.send_message(
            "❌ You do not have permission to control playback.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.secondary, custom_id="music:pause")
    async def pause(self, interaction: discord.Interaction, _):
        if not await self._gate(interaction):
            return
        state = self.cog.get_state(self.guild_id)
        vc = state.voice_client
        if vc and vc.is_playing():
            vc.pause()
        elif vc and vc.is_paused():
            vc.resume()
        await interaction.response.defer()

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, _):
        if not await self._gate(interaction):
            return
        state = self.cog.get_state(self.guild_id)
        if state.voice_client:
            state.voice_client.stop()
        await interaction.response.defer()

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop_btn(self, interaction: discord.Interaction, _):
        if not await self._gate(interaction):
            return
        state = self.cog.get_state(self.guild_id)
        state.queue.clear()
        if state.voice_client:
            state.voice_client.stop()
            await state.voice_client.disconnect(force=False)
            state.voice_client = None
        state.current = None
        await self.cog.refresh_player(interaction.guild)
        await interaction.response.defer()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop")
    async def loop_btn(self, interaction: discord.Interaction, _):
        if not await self._gate(interaction):
            return
        state = self.cog.get_state(self.guild_id)
        state.loop = not state.loop
        await self.cog.refresh_player(interaction.guild)
        await interaction.response.defer()

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle")
    async def shuffle_btn(self, interaction: discord.Interaction, _):
        if not await self._gate(interaction):
            return
        import random
        state = self.cog.get_state(self.guild_id)
        random.shuffle(state.queue)
        await self.cog.refresh_player(interaction.guild)
        await interaction.response.defer()


# ---------- the cog ---------------------------------------------------------

class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}

    def get_state(self, guild_id: int) -> GuildMusicState:
        st = self.states.get(guild_id)
        if st is None:
            st = GuildMusicState()
            self.states[guild_id] = st
        return st

    # ---- /play -----------------------------------------------------------
    @app_commands.command(name="play", description="Search and play a song in Grand Auditorium")
    @app_commands.describe(query="Song name or YouTube URL")
    async def play(self, interaction: discord.Interaction, query: str):
        # restrict to the song-request channel
        if interaction.channel and interaction.channel.name != SONG_REQUEST_CHANNEL_NAME:
            await interaction.response.send_message(
                f"❌ Use this command in #{SONG_REQUEST_CHANNEL_NAME}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            results = await asyncio.to_thread(_search_sync, query)
        except Exception as e:
            await interaction.followup.send(f"⚠️ Search failed: `{e}`", ephemeral=True)
            return

        if not results:
            await interaction.followup.send("😕 No results.", ephemeral=True)
            return

        view = SearchView(self, interaction.user, results, page=0)  # type: ignore
        await interaction.followup.send(embed=view.render_embed(), view=view)

    # ---- selection -> queue ---------------------------------------------
    async def handle_pick(self, interaction: discord.Interaction,
                          requester: discord.Member, result: dict,
                          self_message: discord.Message | None):
        webpage_url = result.get("webpage_url") or result.get("url")
        if not webpage_url:
            await interaction.followup.send("⚠️ Could not resolve that result.", ephemeral=True)
            return

        try:
            info = await asyncio.to_thread(_resolve_stream_sync, webpage_url)
        except Exception as e:
            await interaction.followup.send(f"⚠️ Could not load track: `{e}`", ephemeral=True)
            return

        track = Track(
            title=info.get("title") or "Unknown",
            url=info.get("url") or webpage_url,
            webpage_url=webpage_url,
            duration=int(info.get("duration") or 0),
            requester_id=requester.id,
            requester_mention=requester.mention,
        )

        state = self.get_state(interaction.guild_id)
        state.queue.append(track)

        # delete the search message — the permanent player will show queue state
        if self_message:
            try:
                await self_message.delete()
            except Exception:
                pass

        # ensure voice + start playback if idle
        await self._ensure_voice(interaction.guild, requester)
        if state.voice_client and not state.voice_client.is_playing() and state.current is None:
            await self._play_next(interaction.guild)
        else:
            await self.refresh_player(interaction.guild)

    # ---- voice connection ------------------------------------------------
    async def _ensure_voice(self, guild: discord.Guild, member: discord.Member):
        state = self.get_state(guild.id)

        target = discord.utils.get(guild.voice_channels, name=ALLOWED_VOICE_CHANNEL_NAME)
        if target is None:
            raise RuntimeError(f"Voice channel '{ALLOWED_VOICE_CHANNEL_NAME}' not found.")

        if state.voice_client and state.voice_client.is_connected():
            if state.voice_client.channel.id != target.id:
                # never play anywhere else — move back
                await state.voice_client.move_to(target)
            return

        state.voice_client = await target.connect(self_deaf=True)

    # ---- playback loop --------------------------------------------------
    async def _play_next(self, guild: discord.Guild):
        state = self.get_state(guild.id)
        if not state.voice_client:
            return

        if state.loop and state.current:
            track = state.current
        else:
            if not state.queue:
                state.current = None
                await self.refresh_player(guild)
                return
            track = state.queue.pop(0)
            state.current = track

        def after(_err):
            fut = asyncio.run_coroutine_threadsafe(self._play_next(guild), self.bot.loop)
            try:
                fut.result()
            except Exception:
                pass

        source = discord.FFmpegPCMAudio(
            track.url,
            before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            options="-vn",
        )
        state.voice_client.play(source, after=after)
        await self.refresh_player(guild)

    # ---- permanent player embed -----------------------------------------
    async def refresh_player(self, guild: discord.Guild):
        state = self.get_state(guild.id)

        channel = discord.utils.get(guild.text_channels, name=SONG_REQUEST_CHANNEL_NAME)
        if channel is None:
            return

        emb = discord.Embed(color=discord.Color.green())
        if state.current:
            emb.title = "🎵 Now Playing"
            emb.description = (
                f"**{state.current.title}**\n\n"
                f"Requested by: {state.current.requester_mention}"
            )
        else:
            emb.title = "🎵 Player Idle"
            emb.description = "_Queue is empty. Use `/play <song>` to add one._"

        if state.queue:
            up_next = "\n".join(
                f"{i+1}. **{t.title}** — {t.requester_mention}"
                for i, t in enumerate(state.queue[:10])
            )
            emb.add_field(name="Next Up", value=up_next, inline=False)

        flags = []
        if state.loop:
            flags.append("🔁 Loop ON")
        if flags:
            emb.set_footer(text=" • ".join(flags))

        view = PlayerControls(self, guild.id)

        # edit existing permanent message if we have it
        if state.player_message_id and state.player_channel_id:
            try:
                old_channel = guild.get_channel(state.player_channel_id) or channel
                msg = await old_channel.fetch_message(state.player_message_id)
                await msg.edit(embed=emb, view=view)
                return
            except Exception:
                pass

        # otherwise send once and remember it
        msg = await channel.send(embed=emb, view=view)
        state.player_message_id = msg.id
        state.player_channel_id = channel.id


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))

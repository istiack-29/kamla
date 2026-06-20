import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import yt_dlp
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# Load Opus — system libopus installed via render.yaml aptPackages
if not discord.opus.is_loaded():
    _OPUS_PATHS = [
        "libopus.so.0",
        "libopus.so",
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/aarch64-linux-gnu/libopus.so.0",
        "/usr/lib/libopus.so.0",
        "opus",
    ]
    for _path in _OPUS_PATHS:
        try:
            discord.opus.load_opus(_path)
            print(f"[Music] Opus loaded: {_path}")
            break
        except Exception:
            continue
    if not discord.opus.is_loaded():
        print("[Music] Opus not loaded via system paths — davey (bundled) will handle encoding.")

print(f"[Music] Opus status: is_loaded={discord.opus.is_loaded()}")

SONG_REQUEST_CHANNEL = "🎼︱song-request"
GRAND_AUDITORIUM_VC  = "Grand Auditorium"

PLAYBACK_ALLOWED_ROLES = {"ORG", "CAP"}
RESULTS_PER_PAGE = 4

YTDL_SEARCH_OPTS = {
    "format":         "bestaudio/best",
    "noplaylist":     True,
    "quiet":          True,
    "no_warnings":    True,
    "extract_flat":   True,
    "default_search": "ytsearch10",
}

YTDL_STREAM_OPTS = {
    "format":          "bestaudio/best",
    "noplaylist":      True,
    "quiet":           True,
    "no_warnings":     True,
    "source_address":  "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options":        "-vn",
}

NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


@dataclass
class Track:
    title:       str
    url:         str
    duration:    str
    requester:   discord.Member
    webpage_url: str = ""


@dataclass
class GuildMusicState:
    queue:        deque = field(default_factory=deque)
    current:      Optional[Track] = None
    player_msg:   Optional[discord.Message] = None
    loop:         bool = False
    shuffle:      bool = False
    paused:       bool = False
    voice_client: Optional[discord.VoiceClient] = None


_states: dict[int, GuildMusicState] = {}


def get_state(guild_id: int) -> GuildMusicState:
    if guild_id not in _states:
        _states[guild_id] = GuildMusicState()
    return _states[guild_id]


def fmt_duration(seconds) -> str:
    try:
        s = int(seconds)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"
    except Exception:
        return "?:??"


def _can_control(interaction: discord.Interaction, state: GuildMusicState) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    if any(r.name in PLAYBACK_ALLOWED_ROLES for r in interaction.user.roles):
        return True
    if state.current and state.current.requester.id == interaction.user.id:
        return True
    return False


def _is_song_request_channel(channel: discord.abc.GuildChannel) -> bool:
    if not isinstance(channel, discord.TextChannel):
        return False
    return "song-request" in channel.name.lower()


async def _search_youtube(query: str) -> list[dict]:
    loop = asyncio.get_event_loop()
    def _do():
        with yt_dlp.YoutubeDL(YTDL_SEARCH_OPTS) as ydl:
            result = ydl.extract_info(f"ytsearch10:{query}", download=False)
            if result and "entries" in result:
                return [e for e in result["entries"] if e]
        return []
    return await loop.run_in_executor(None, _do)


async def _get_stream_url(webpage_url: str) -> str:
    loop = asyncio.get_event_loop()
    def _do():
        with yt_dlp.YoutubeDL(YTDL_STREAM_OPTS) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
            return info.get("url", "")
    return await loop.run_in_executor(None, _do)


def _build_player_embed(state: GuildMusicState) -> discord.Embed:
    if not state.current:
        return discord.Embed(
            title="🎵 No Music Playing",
            description="Use `/play <song name>` to add a song to the queue.",
            color=discord.Color.greyple(),
        )

    track  = state.current
    status = "⏸ Paused" if state.paused else "▶ Playing"
    flags  = []
    if state.loop:    flags.append("🔁 Loop")
    if state.shuffle: flags.append("🔀 Shuffle")
    flag_str = "  " + "  ".join(flags) if flags else ""

    embed = discord.Embed(
        title=f"🎵 Now Playing{flag_str}",
        description=f"**[{track.title}]({track.webpage_url})**",
        color=discord.Color.green() if not state.paused else discord.Color.orange(),
    )
    embed.add_field(name="Requested by", value=track.requester.mention, inline=True)
    embed.add_field(name="Duration",     value=track.duration,          inline=True)
    embed.add_field(name="Status",       value=status,                  inline=True)

    if state.queue:
        lines = [f"`{i}.` {t.title} — {t.requester.mention}" for i, t in enumerate(list(state.queue)[:5], 1)]
        if len(state.queue) > 5:
            lines.append(f"*…and {len(state.queue) - 5} more*")
        embed.add_field(name="Next Up", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Next Up", value="*Queue is empty*", inline=False)

    embed.set_footer(text="Controls: ⏯ Pause  ⏭ Skip  ⏹ Stop  🔁 Loop  🔀 Shuffle")
    return embed


class PlayerControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        state = get_state(interaction.guild_id)
        if not _can_control(interaction, state):
            await interaction.response.send_message(
                "❌ You do not have permission to control playback.\n"
                "Only the current requester, ORG, or CAP may control music.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.primary,  custom_id="kamla:music:pause",   row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        state = get_state(interaction.guild_id)
        vc = state.voice_client
        if not vc:
            await interaction.response.send_message("❌ Not connected to voice.", ephemeral=True)
            return
        if vc.is_paused():
            vc.resume()
            state.paused = False
        elif vc.is_playing():
            vc.pause()
            state.paused = True
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
            return
        await interaction.response.defer()
        await _update_player(interaction.guild)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, custom_id="kamla:music:skip",    row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        state = get_state(interaction.guild_id)
        vc = state.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("❌ Nothing to skip.", ephemeral=True)
            return
        vc.stop()
        await interaction.response.send_message("⏭ Skipped.", ephemeral=True, delete_after=3)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger,    custom_id="kamla:music:stop",    row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        state = get_state(interaction.guild_id)
        vc = state.voice_client
        state.queue.clear()
        state.current = None
        state.loop = False
        state.shuffle = False
        state.paused = False
        if vc:
            vc.stop()
            await vc.disconnect()
            state.voice_client = None
        await interaction.response.defer()
        await _update_player(interaction.guild)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="kamla:music:loop",    row=0)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        state = get_state(interaction.guild_id)
        state.loop = not state.loop
        await interaction.response.send_message(
            f"🔁 Loop {'**ON**' if state.loop else '**OFF**'}.", ephemeral=True, delete_after=4
        )
        await _update_player(interaction.guild)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="kamla:music:shuffle", row=0)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        state = get_state(interaction.guild_id)
        state.shuffle = not state.shuffle
        await interaction.response.send_message(
            f"🔀 Shuffle {'**ON**' if state.shuffle else '**OFF**'}.", ephemeral=True, delete_after=4
        )
        await _update_player(interaction.guild)


async def _update_player(guild: discord.Guild) -> None:
    state = get_state(guild.id)
    if not state.player_msg:
        return
    try:
        await state.player_msg.edit(embed=_build_player_embed(state), view=PlayerControlView())
    except Exception as e:
        print(f"[Music] Failed to update player embed: {e}")


async def _play_next(guild: discord.Guild, bot: commands.Bot) -> None:
    state = get_state(guild.id)
    vc = state.voice_client

    if not vc or not vc.is_connected():
        state.current = None
        await _update_player(guild)
        return

    if state.loop and state.current:
        track = state.current
    elif state.queue:
        if state.shuffle:
            import random
            items = list(state.queue)
            track = items.pop(random.randrange(len(items)))
            state.queue = deque(items)
        else:
            track = state.queue.popleft()
        state.current = track
    else:
        state.current = None
        await _update_player(guild)
        await asyncio.sleep(120)
        if not state.current and vc.is_connected() and not vc.is_playing():
            await vc.disconnect()
            state.voice_client = None
        return

    try:
        stream_url = await _get_stream_url(track.webpage_url or track.url)
        if not stream_url:
            print(f"[Music] ERROR: Empty stream URL for: {track.title}")
            state.current = None
            await _update_player(guild)
            return

        print(f"[Music] Starting playback: {track.title}")
        source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
        source = discord.PCMVolumeTransformer(source, volume=0.8)

        def after(error):
            if error:
                print(f"[Music] Playback error: {error}")
            asyncio.run_coroutine_threadsafe(_play_next(guild, bot), bot.loop)

        vc.play(source, after=after)
        state.paused = False
        print(f"[Music] vc.is_playing() = {vc.is_playing()}")
        await _update_player(guild)
    except Exception as e:
        print(f"[Music] Stream error for '{track.title}': {e}")
        state.current = None
        await _update_player(guild)


class SearchResultView(discord.ui.View):
    def __init__(self, results, requester, guild, bot, page=0):
        super().__init__(timeout=60)
        self.results   = results
        self.requester = requester
        self.guild     = guild
        self.bot       = bot
        self.page      = page
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        start = self.page * RESULTS_PER_PAGE
        end   = min(start + RESULTS_PER_PAGE, len(self.results))

        for i, entry in enumerate(self.results[start:end]):
            global_idx = start + i
            emoji = NUMBER_EMOJIS[global_idx] if global_idx < len(NUMBER_EMOJIS) else f"{global_idx+1}"
            btn = discord.ui.Button(emoji=emoji, style=discord.ButtonStyle.primary,
                                    custom_id=f"search_select_{global_idx}")
            btn.callback = self._make_select_callback(global_idx)
            self.add_item(btn)

        total_pages = (len(self.results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE

        if self.page > 0:
            prev = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, custom_id="search_prev")
            prev.callback = self._prev_callback
            self.add_item(prev)

        if self.page < total_pages - 1:
            nxt = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.secondary, custom_id="search_next")
            nxt.callback = self._next_callback
            self.add_item(nxt)

        cancel = discord.ui.Button(label="✖ Cancel", style=discord.ButtonStyle.danger, custom_id="search_cancel")
        cancel.callback = self._cancel_callback
        self.add_item(cancel)

    def _make_select_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.requester.id:
                await interaction.response.send_message("❌ Only the requester can interact.", ephemeral=True)
                return
            entry = self.results[idx]
            duration    = fmt_duration(entry.get("duration", 0))
            webpage_url = entry.get("url", "") or entry.get("webpage_url", "")
            if not webpage_url.startswith("http"):
                webpage_url = f"https://www.youtube.com/watch?v={entry.get('id', '')}"
            track = Track(
                title=entry.get("title", "Unknown"),
                url=webpage_url,
                duration=duration,
                requester=self.requester,
                webpage_url=webpage_url,
            )
            state = get_state(self.guild.id)
            state.queue.append(track)
            self.stop()
            try:
                await interaction.message.delete()
            except Exception:
                pass
            await interaction.response.send_message(
                f"✅ Added **{track.title}** to the queue.", ephemeral=True, delete_after=5
            )
            if not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
                vc = await _ensure_voice(self.guild, self.bot)
                if vc:
                    await _play_next(self.guild, self.bot)
            else:
                await _update_player(self.guild)
        return callback

    async def _prev_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("❌ Only the requester can interact.", ephemeral=True)
            return
        self.page -= 1
        self._build_buttons()
        await interaction.response.edit_message(embed=_build_search_embed(self.results, self.page), view=self)

    async def _next_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("❌ Only the requester can interact.", ephemeral=True)
            return
        self.page += 1
        self._build_buttons()
        await interaction.response.edit_message(embed=_build_search_embed(self.results, self.page), view=self)

    async def _cancel_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("❌ Only the requester can interact.", ephemeral=True)
            return
        self.stop()
        try:
            await interaction.message.delete()
        except Exception:
            pass
        await interaction.response.send_message("❌ Search cancelled.", ephemeral=True, delete_after=3)

    async def on_timeout(self):
        try:
            await self.message.delete()
        except Exception:
            pass


def _build_search_embed(results: list[dict], page: int) -> discord.Embed:
    start       = page * RESULTS_PER_PAGE
    end         = min(start + RESULTS_PER_PAGE, len(results))
    total_pages = (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE

    embed = discord.Embed(
        title=f"🔎 Search Results — Page {page + 1}/{total_pages}",
        color=discord.Color.blurple(),
    )
    lines = []
    for i in range(start, end):
        entry  = results[i]
        emoji  = NUMBER_EMOJIS[i] if i < len(NUMBER_EMOJIS) else f"{i+1}."
        dur    = fmt_duration(entry.get("duration", 0))
        title  = entry.get("title", "Unknown")
        vid_id = entry.get("id", "")
        url    = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""
        lines.append(f"{emoji} **[{title}]({url})** `{dur}`" if url else f"{emoji} **{title}** `{dur}`")

    embed.description = "\n".join(lines)
    embed.set_footer(text="Click a number to add that song to the queue.")
    return embed


async def _ensure_voice(guild: discord.Guild, bot: commands.Bot) -> Optional[discord.VoiceClient]:
    state = get_state(guild.id)
    if state.voice_client and state.voice_client.is_connected():
        return state.voice_client
    vc_channel = discord.utils.get(guild.voice_channels, name=GRAND_AUDITORIUM_VC)
    if not vc_channel:
        return None
    try:
        vc = await vc_channel.connect()
        state.voice_client = vc
        return vc
    except Exception as e:
        print(f"[Music] Voice connect error: {e}")
        return None


async def _get_or_create_player_msg(guild: discord.Guild, bot: commands.Bot) -> Optional[discord.Message]:
    state   = get_state(guild.id)
    song_ch = None
    for ch in guild.text_channels:
        if "song-request" in ch.name:
            song_ch = ch
            break
    if not song_ch:
        return None

    if state.player_msg:
        try:
            return await song_ch.fetch_message(state.player_msg.id)
        except Exception:
            state.player_msg = None

    try:
        msg = await song_ch.send(embed=_build_player_embed(state), view=PlayerControlView())
        state.player_msg = msg
        return msg
    except Exception as e:
        print(f"[Music] Failed to send player embed: {e}")
        return None


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not _is_song_request_channel(message.channel):
            return
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.channel.send(
                f"⛔ {message.author.mention} — এই চ্যানেলে শুধু `/play` কমান্ড ব্যবহার করা যাবে।\n"
                "Only `/play <song name or URL>` is allowed here.",
                delete_after=6,
            )
        except Exception:
            pass

    @app_commands.command(name="play", description="Search for a song and add it to the queue.")
    @app_commands.describe(query="Song name or YouTube URL")
    async def play(self, interaction: discord.Interaction, query: str):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return

        song_ch = None
        for ch in interaction.guild.text_channels:
            if "song-request" in ch.name:
                song_ch = ch
                break

        if song_ch and interaction.channel_id != song_ch.id:
            await interaction.response.send_message(
                f"❌ Please use `/play` in {song_ch.mention} only.", ephemeral=True,
            )
            return

        state = get_state(interaction.guild_id)
        if not state.player_msg:
            await _get_or_create_player_msg(interaction.guild, self.bot)

        await interaction.response.defer(ephemeral=True, thinking=True)

        results = await _search_youtube(query)
        if not results:
            await interaction.followup.send("❌ No results found. Try a different search.", ephemeral=True)
            return

        embed = _build_search_embed(results, 0)
        view  = SearchResultView(results, interaction.user, interaction.guild, self.bot, page=0)

        if song_ch:
            msg = await song_ch.send(embed=embed, view=view)
            view.message = msg
            await interaction.followup.send("✅ Search results posted in the channel.", ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="start", description="Set up the music channels in this server.")
    @app_commands.default_permissions(administrator=True)
    async def start(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Must be used in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        ga_cat = discord.utils.get(guild.categories, name="🏟️︱GRAND AUDITORIUM")
        roles  = {r.name: r for r in guild.roles}

        from server_builder import _build_song_request_overwrites, _build_all_role_overwrites

        if ga_cat is None:
            ga_cat = await guild.create_category(
                "🏟️︱GRAND AUDITORIUM", overwrites=_build_all_role_overwrites(guild, roles)
            )

        song_ch = None
        for ch in ga_cat.text_channels:
            if "song-request" in ch.name:
                song_ch = ch
                break
        if not song_ch:
            song_ch = await guild.create_text_channel(
                "🎼︱song-request", category=ga_cat,
                overwrites=_build_song_request_overwrites(guild, roles),
            )

        vc = discord.utils.get(ga_cat.voice_channels, name=GRAND_AUDITORIUM_VC)
        if not vc:
            vc = await guild.create_voice_channel(
                GRAND_AUDITORIUM_VC, category=ga_cat,
                overwrites=_build_all_role_overwrites(guild, roles),
            )

        await interaction.followup.send(
            f"✅ Music channels ready!\n• Text: {song_ch.mention}\n• Voice: **{vc.name}**\n\n"
            f"Use `/play <song name>` in {song_ch.mention} to get started.",
            ephemeral=True,
        )

    @app_commands.command(name="queue", description="Show the current music queue.")
    async def queue_cmd(self, interaction: discord.Interaction):
        state = get_state(interaction.guild_id)
        if not state.current and not state.queue:
            await interaction.response.send_message("📭 The queue is empty.", ephemeral=True)
            return

        lines = []
        if state.current:
            lines.append(f"**Now Playing:** {state.current.title} — {state.current.requester.mention}")
        for i, t in enumerate(list(state.queue), 1):
            lines.append(f"`{i}.` {t.title} — {t.requester.mention}")

        embed = discord.Embed(
            title="🎵 Music Queue",
            description="\n".join(lines) or "*Empty*",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        guild = member.guild
        state = get_state(guild.id)
        vc = state.voice_client
        if not vc or not vc.is_connected():
            return
        if len(vc.channel.members) == 1:
            await asyncio.sleep(30)
            if len(vc.channel.members) == 1:
                state.queue.clear()
                state.current = None
                state.loop = False
                state.shuffle = False
                state.paused = False
                await vc.disconnect()
                state.voice_client = None
                await _update_player(guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))

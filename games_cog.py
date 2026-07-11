"""
games_cog.py — Tic-Tac-Toe and Rock-Paper-Scissors mini-games for KAMLA.

Both games share a single `/play` slash command that behaves differently
depending on which channel it is used in:
  • "❌︱tic-tac-toe︱⭕"             → Tic-Tac-Toe challenge
  • "🤛︱rock✊-paper📰-scissors✌️"   → Rock-Paper-Scissors challenge (best of `times`)

All game state lives in memory only — no database is used anywhere in this
cog. State is removed as soon as a challenge is rejected/expires or a match
finishes, so long-running processes do not leak memory. If the bot restarts
mid-match, the in-memory state (and therefore the match) is lost by design;
this is expected and acceptable since there is nowhere else to persist it.
"""

import asyncio
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

TTT_CHANNEL_NAME = "❌︱tic-tac-toe︱⭕"
RPS_CHANNEL_NAME = "🤛︱rock✊-paper📰-scissors✌️"

TTT_FILTER_MSG = "This channel is only for Tic-Tac-Toe matches."
RPS_FILTER_MSG = "This channel is only for Rock-Paper-Scissors matches."

CHALLENGE_TIMEOUT = 120  # seconds to accept/reject a challenge
TTT_TURN_TIMEOUT = 60    # seconds per Tic-Tac-Toe turn

NUM_EMOJI = ["0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


def _round_label(n: int) -> str:
    """Render a positive integer using number emojis, e.g. 12 -> '1️⃣2️⃣'."""
    return "".join(NUM_EMOJI[int(d)] for d in str(n))


# ──────────────────────────────────────────────────────────────────────────
# Shared "busy" tracking — prevents a user from being in two games/challenges
# at once, and prevents race conditions from double-challenges.
# ──────────────────────────────────────────────────────────────────────────

_busy: set[tuple[int, int]] = set()  # (guild_id, user_id)


def _mark_busy(guild_id: int, *user_ids: int) -> None:
    for uid in user_ids:
        _busy.add((guild_id, uid))


def _clear_busy(guild_id: int, *user_ids: int) -> None:
    for uid in user_ids:
        _busy.discard((guild_id, uid))


def _is_busy(guild_id: int, user_id: int) -> bool:
    return (guild_id, user_id) in _busy


def _challenge_embed(challenger: discord.Member, opponent: discord.Member, title: str) -> discord.Embed:
    now = datetime.now(timezone.utc)
    embed = discord.Embed(
        title=title,
        description=f"{challenger.mention} challenged {opponent.mention}",
        color=discord.Color.blurple(),
        timestamp=now,
    )
    embed.set_thumbnail(url=challenger.display_avatar.url)
    embed.add_field(name="Date", value=f"<t:{int(now.timestamp())}:D>", inline=True)
    embed.add_field(name="Time", value=f"<t:{int(now.timestamp())}:T>", inline=True)
    return embed


def _apply_rejection(embed: discord.Embed, rejector: discord.Member) -> discord.Embed:
    embed.add_field(name="ALLOCATION", value=f"Rejected by {rejector.mention}", inline=False)
    embed.color = discord.Color.red()
    return embed


async def _send_challenge_and_cleanup_response(
    interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View
) -> discord.Message:
    """Post the challenge as a normal channel message and remove the ephemeral
    command acknowledgement, per the 'delete original response' requirement."""
    await interaction.response.send_message("📨 Challenge sent.", ephemeral=True)
    msg = await interaction.channel.send(embed=embed, view=view)
    try:
        await interaction.delete_original_response()
    except Exception:
        pass
    return msg


# ──────────────────────────────────────────────────────────────────────────
# Tic-Tac-Toe
# ──────────────────────────────────────────────────────────────────────────


class TicTacToeGame:
    """Mutable state for a single Tic-Tac-Toe match."""

    def __init__(self, guild_id: int, o_member: discord.Member, x_member: discord.Member, first: discord.Member):
        self.guild_id = guild_id
        self.board: list[str | None] = [None] * 9
        self.symbol: dict[int, str] = {o_member.id: "⭕", x_member.id: "❌"}
        self.turn = first.id
        self.message: discord.Message | None = None
        self.finished = False
        self.timeout_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    def other(self, user_id: int) -> int:
        for uid in self.symbol:
            if uid != user_id:
                return uid
        return user_id

    def winner(self) -> str | None:
        lines = [
            (0, 1, 2), (3, 4, 5), (6, 7, 8),
            (0, 3, 6), (1, 4, 7), (2, 5, 8),
            (0, 4, 8), (2, 4, 6),
        ]
        for a, b, c in lines:
            if self.board[a] and self.board[a] == self.board[b] == self.board[c]:
                return self.board[a]
        return None

    def is_draw(self) -> bool:
        return all(cell is not None for cell in self.board) and self.winner() is None


def _build_ttt_embed(game: TicTacToeGame, result_text: str | None = None) -> discord.Embed:
    o_id = next(uid for uid, s in game.symbol.items() if s == "⭕")
    x_id = next(uid for uid, s in game.symbol.items() if s == "❌")

    lines = ["**ALLOCATION:**", f"<@{o_id}> ⭕", f"<@{x_id}> ❌", ""]
    if result_text:
        lines.append(result_text)
    elif all(cell is None for cell in game.board):
        lines.append(f"{game.symbol[game.turn]} First")
    else:
        lines.append(f"**Turn:** <@{game.turn}> {game.symbol[game.turn]}")

    return discord.Embed(
        title="❌⭕ Tic-Tac-Toe",
        description="\n".join(lines),
        color=discord.Color.green() if game.finished else discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )


def _reschedule_ttt_timeout(game: TicTacToeGame, view: "TicTacToeView") -> None:
    if game.timeout_task and not game.timeout_task.done():
        game.timeout_task.cancel()
    game.timeout_task = asyncio.create_task(_ttt_timeout_watchdog(game, view))


async def _ttt_timeout_watchdog(game: TicTacToeGame, view: "TicTacToeView") -> None:
    try:
        await asyncio.sleep(TTT_TURN_TIMEOUT)
    except asyncio.CancelledError:
        return
    if game.finished or game.message is None:
        return
    async with game.lock:
        if game.finished:
            return
        game.finished = True
        winner_id = game.other(game.turn)
        for child in view.children:
            child.disabled = True
        _clear_busy(game.guild_id, *game.symbol.keys())
        embed = _build_ttt_embed(game, result_text=f"**Result:**\n<@{winner_id}> wins (via opposition timeout)")
        try:
            await game.message.edit(embed=embed, view=view)
        except Exception:
            pass


async def _finish_ttt(game: TicTacToeGame, view: "TicTacToeView", interaction: discord.Interaction, result_text: str) -> None:
    game.finished = True
    if game.timeout_task and not game.timeout_task.done():
        game.timeout_task.cancel()
    for child in view.children:
        child.disabled = True
    _clear_busy(game.guild_id, *game.symbol.keys())
    embed = _build_ttt_embed(game, result_text=result_text)
    await interaction.response.edit_message(embed=embed, view=view)


class TTTCellButton(discord.ui.Button):
    def __init__(self, game: TicTacToeGame, index: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=index // 3)
        self.game = game
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        game = self.game

        if interaction.user.id not in game.symbol:
            await interaction.response.send_message(
                "⛔ Only the two participants of this match may press these buttons.", ephemeral=True
            )
            return
        if game.finished:
            await interaction.response.send_message("This match has already ended.", ephemeral=True)
            return
        if interaction.user.id != game.turn:
            await interaction.response.send_message("⏳ It's not your turn.", ephemeral=True)
            return

        async with game.lock:
            if game.finished or game.board[self.index] is not None:
                await interaction.response.send_message("That cell is already taken.", ephemeral=True)
                return

            symbol = game.symbol[interaction.user.id]
            game.board[self.index] = symbol
            self.label = symbol
            self.disabled = True
            self.style = discord.ButtonStyle.success if symbol == "⭕" else discord.ButtonStyle.danger

            view: TicTacToeView = self.view  # type: ignore[assignment]

            winner_symbol = game.winner()
            if winner_symbol:
                await _finish_ttt(game, view, interaction, f"**Result:**\n<@{interaction.user.id}> wins")
                return
            if game.is_draw():
                await _finish_ttt(game, view, interaction, "**Result:**\nDRAW!!")
                return

            game.turn = game.other(interaction.user.id)
            _reschedule_ttt_timeout(game, view)
            await interaction.response.edit_message(embed=_build_ttt_embed(game), view=view)


class TicTacToeView(discord.ui.View):
    def __init__(self, game: TicTacToeGame):
        super().__init__(timeout=None)
        self.game = game
        for i in range(9):
            self.add_item(TTTCellButton(game, i))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.game.symbol:
            await interaction.response.send_message(
                "⛔ Only the two participants of this match may press these buttons.", ephemeral=True
            )
            return False
        return True


class TTTChallengeView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member):
        super().__init__(timeout=CHALLENGE_TIMEOUT)
        self.challenger = challenger
        self.opponent = opponent
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message(
                "⛔ Only the challenged user may respond to this challenge.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        _clear_busy(self.challenger.guild.id, self.challenger.id, self.opponent.id)
        if self.message:
            try:
                for child in self.children:
                    child.disabled = True
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        o_member, x_member = random.sample([self.challenger, self.opponent], 2)
        first = random.choice([o_member, x_member])
        game = TicTacToeGame(interaction.guild.id, o_member, x_member, first)
        view = TicTacToeView(game)
        await interaction.response.edit_message(embed=_build_ttt_embed(game), view=view)
        game.message = interaction.message
        _reschedule_ttt_timeout(game, view)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        _clear_busy(interaction.guild.id, self.challenger.id, self.opponent.id)
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed = _apply_rejection(embed, self.opponent)
        await interaction.response.edit_message(embed=embed, view=self)


async def _handle_ttt_challenge(interaction: discord.Interaction, opponent: discord.Member) -> None:
    guild = interaction.guild
    challenger = interaction.user

    if opponent.id == challenger.id:
        await interaction.response.send_message("⛔ You cannot challenge yourself.", ephemeral=True)
        return
    if opponent.bot:
        await interaction.response.send_message("⛔ You cannot challenge a bot.", ephemeral=True)
        return
    if _is_busy(guild.id, challenger.id):
        await interaction.response.send_message(
            "⛔ You are already in a match or have a pending challenge.", ephemeral=True
        )
        return
    if _is_busy(guild.id, opponent.id):
        await interaction.response.send_message(
            f"⛔ {opponent.mention} is already in a match or has a pending challenge.", ephemeral=True
        )
        return

    _mark_busy(guild.id, challenger.id, opponent.id)

    embed = _challenge_embed(challenger, opponent, "❌⭕ Tic-Tac-Toe Challenge")
    view = TTTChallengeView(challenger, opponent)
    msg = await _send_challenge_and_cleanup_response(interaction, embed, view)
    view.message = msg


# ──────────────────────────────────────────────────────────────────────────
# Rock-Paper-Scissors
# ──────────────────────────────────────────────────────────────────────────

_RPS_BEATS = {"✊": "✌️", "✌️": "📰", "📰": "✊"}


class RPSGame:
    def __init__(self, guild_id: int, challenger: discord.Member, opponent: discord.Member, times: int):
        self.guild_id = guild_id
        self.players = [challenger, opponent]
        self.times = times
        self.round = 0
        self.score: dict[int, int] = {challenger.id: 0, opponent.id: 0}
        self.round_results: list[str] = [""] * times
        self.choices: dict[int, str] = {}
        self.message: discord.Message | None = None
        self.finished = False
        self.lock = asyncio.Lock()


def _build_rps_embed(game: RPSGame, note: str | None = None) -> discord.Embed:
    lines = []
    for i in range(game.times):
        status = game.round_results[i] or ("ongoing..." if i == game.round and not game.finished else "coming")
        lines.append(f"ROUND{_round_label(i + 1)}: {status}")

    embed = discord.Embed(
        title="✊📰✌️ Rock-Paper-Scissors",
        description="\n".join(lines),
        color=discord.Color.green() if game.finished else discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    if note:
        embed.add_field(name="Note", value=note, inline=False)
    if game.finished:
        a, b = sorted(game.players, key=lambda m: -game.score[m.id])
        embed.add_field(
            name="Result",
            value=f"{a.mention} {_round_label(game.score[a.id])}-{_round_label(game.score[b.id])} {b.mention}",
            inline=False,
        )
    return embed


async def _resolve_rps_round(game: RPSGame, view: "RPSChoiceView") -> None:
    p1, p2 = game.players
    c1, c2 = game.choices[p1.id], game.choices[p2.id]

    if c1 == c2:
        game.choices.clear()
        embed = _build_rps_embed(game, note=f"Round {game.round + 1} was a draw ({c1} vs {c2}) — play it again.")
        try:
            await game.message.edit(embed=embed, view=view)
        except Exception:
            pass
        return

    winner = p1 if _RPS_BEATS[c1] == c2 else p2
    game.score[winner.id] += 1
    game.round_results[game.round] = f"{winner.mention} win"
    game.round += 1
    game.choices.clear()

    if game.round >= game.times:
        game.finished = True
        for child in view.children:
            child.disabled = True
        _clear_busy(game.guild_id, p1.id, p2.id)

    embed = _build_rps_embed(game)
    try:
        await game.message.edit(embed=embed, view=view)
    except Exception:
        pass


class RPSChoiceView(discord.ui.View):
    def __init__(self, game: RPSGame):
        super().__init__(timeout=None)
        self.game = game

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.game.score:
            await interaction.response.send_message(
                "⛔ Only the two participants of this match may press these buttons.", ephemeral=True
            )
            return False
        return True

    async def _choose(self, interaction: discord.Interaction, choice: str) -> None:
        game = self.game
        if game.finished:
            await interaction.response.send_message("This match has already ended.", ephemeral=True)
            return

        async with game.lock:
            if interaction.user.id in game.choices:
                await interaction.response.send_message(
                    "You already chose for this round. Waiting for your opponent…", ephemeral=True
                )
                return

            game.choices[interaction.user.id] = choice
            await interaction.response.send_message(f"✅ Your choice ({choice}) is locked in.", ephemeral=True)

            if len(game.choices) == 2:
                await _resolve_rps_round(game, self)

    @discord.ui.button(emoji="✊", style=discord.ButtonStyle.secondary)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "✊")

    @discord.ui.button(emoji="📰", style=discord.ButtonStyle.secondary)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "📰")

    @discord.ui.button(emoji="✌️", style=discord.ButtonStyle.secondary)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "✌️")


class RPSChallengeView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, times: int):
        super().__init__(timeout=CHALLENGE_TIMEOUT)
        self.challenger = challenger
        self.opponent = opponent
        self.times = times
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message(
                "⛔ Only the challenged user may respond to this challenge.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        _clear_busy(self.challenger.guild.id, self.challenger.id, self.opponent.id)
        if self.message:
            try:
                for child in self.children:
                    child.disabled = True
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        game = RPSGame(interaction.guild.id, self.challenger, self.opponent, self.times)
        view = RPSChoiceView(game)
        await interaction.response.edit_message(embed=_build_rps_embed(game), view=view)
        game.message = interaction.message

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        _clear_busy(interaction.guild.id, self.challenger.id, self.opponent.id)
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed = _apply_rejection(embed, self.opponent)
        await interaction.response.edit_message(embed=embed, view=self)


async def _handle_rps_challenge(interaction: discord.Interaction, opponent: discord.Member, times: int) -> None:
    guild = interaction.guild
    challenger = interaction.user

    if opponent.id == challenger.id:
        await interaction.response.send_message("⛔ You cannot challenge yourself.", ephemeral=True)
        return
    if opponent.bot:
        await interaction.response.send_message("⛔ You cannot challenge a bot.", ephemeral=True)
        return
    if _is_busy(guild.id, challenger.id):
        await interaction.response.send_message(
            "⛔ You are already in a match or have a pending challenge.", ephemeral=True
        )
        return
    if _is_busy(guild.id, opponent.id):
        await interaction.response.send_message(
            f"⛔ {opponent.mention} is already in a match or has a pending challenge.", ephemeral=True
        )
        return

    _mark_busy(guild.id, challenger.id, opponent.id)

    embed = _challenge_embed(challenger, opponent, "✊📰✌️ Rock-Paper-Scissors Challenge")
    embed.add_field(name="Rounds", value=str(times), inline=True)
    view = RPSChallengeView(challenger, opponent, times)
    msg = await _send_challenge_and_cleanup_response(interaction, embed, view)
    view.message = msg


# ──────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────


class GamesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """/play never appears as a text message — any real message in these
        channels is invalid and gets removed with a short-lived warning."""
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return

        name = message.channel.name
        if name not in (TTT_CHANNEL_NAME, RPS_CHANNEL_NAME):
            return

        try:
            await message.delete()
        except Exception:
            pass

        warning = TTT_FILTER_MSG if name == TTT_CHANNEL_NAME else RPS_FILTER_MSG
        try:
            await message.channel.send(warning, delete_after=6)
        except Exception:
            pass

    @app_commands.command(name="play", description="Challenge another member to a game in this channel.")
    @app_commands.describe(
        user="Member to challenge",
        times="Rock-Paper-Scissors only: number of rounds (2-20, default 1)",
    )
    async def play(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        times: app_commands.Range[int, 2, 20] | None = None,
    ):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command can only be used inside a server text channel.", ephemeral=True
            )
            return

        channel_name = interaction.channel.name
        if channel_name == TTT_CHANNEL_NAME:
            if times is not None:
                await interaction.response.send_message(
                    "⚠️ `times` is only used for Rock-Paper-Scissors.", ephemeral=True
                )
                return
            await _handle_ttt_challenge(interaction, user)
        elif channel_name == RPS_CHANNEL_NAME:
            await _handle_rps_challenge(interaction, user, times or 1)
        else:
            await interaction.response.send_message(
                f"❌ `/play` can only be used in **{TTT_CHANNEL_NAME}** or **{RPS_CHANNEL_NAME}**.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(GamesCog(bot))

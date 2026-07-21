"""
polls_cog.py — Poll and Yes/No voting system for KAMLA.

All poll state is in memory only (`_active_polls`); nothing is written to a
database. Closing a poll removes it from memory but the final embed (with
its last known vote counts) is preserved on the message itself, so nothing
looks broken to end users. If the process restarts, in-flight polls are
lost and their buttons will simply stop responding — acceptable per the
no-database requirement.

• "📊︱poll"           — /newpoll topic:<...> options:<comma list, max 9> for:<optional mentions>
• "📊︱yes-no-voting"  — /newpoll topic:<...> for:<optional mentions>  (fixed YES/NO, always anonymous, single vote)
• /close [where]       — closes an active poll by exact topic, or shows a picker
"""

import re
import uuid
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

POLL_CHANNEL_NAME = "📊︱poll"
YESNO_CHANNEL_NAME = "📊︱yes-no-voting"
POLL_ADMIN_ROLES = {"CAP", "TABBY", "ORG"}
MAX_OPTIONS = 9

NUM_EMOJI = ["0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]

_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")


def _is_poll_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.name in POLL_ADMIN_ROLES for r in member.roles)


class Poll:
    """In-memory representation of a single poll or yes/no vote."""

    def __init__(
        self,
        *,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        topic: str,
        options: list[str],
        multiple_votes: bool,
        anonymous: bool,
        is_yesno: bool,
        role_ids: set[int],
        user_ids: set[int],
    ):
        self.id = uuid.uuid4().hex[:10]
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id: int | None = None
        self.creator_id = creator_id
        self.topic = topic
        self.options = options
        self.multiple_votes = multiple_votes
        self.anonymous = anonymous
        self.is_yesno = is_yesno
        self.role_ids = role_ids
        self.user_ids = user_ids
        self.votes: dict[int, set[int]] = {i: set() for i in range(len(options))}
        self.closed = False

    @property
    def is_open_to_everyone(self) -> bool:
        return not self.role_ids and not self.user_ids

    def is_eligible(self, member: discord.Member) -> bool:
        if self.is_open_to_everyone:
            return True
        if member.id in self.user_ids:
            return True
        return any(r.id in self.role_ids for r in member.roles)


# poll_id -> Poll
_active_polls: dict[str, Poll] = {}


def _guild_open_polls(guild_id: int) -> list[Poll]:
    return [p for p in _active_polls.values() if p.guild_id == guild_id and not p.closed]


def _parse_targets(guild: discord.Guild, raw: str | None) -> tuple[set[int], set[int], list[discord.Member]]:
    """Parse the `for` string (containing real mention tokens) into role ids,
    user ids, and a flat, de-duplicated audience list for DM notifications."""
    if not raw:
        return set(), set(), []

    role_ids = {int(m) for m in _ROLE_MENTION_RE.findall(raw)}
    stripped = _ROLE_MENTION_RE.sub("", raw)  # avoid role tags leaking into the user regex
    user_ids = {int(m) for m in _USER_MENTION_RE.findall(stripped)}

    audience: dict[int, discord.Member] = {}
    for rid in role_ids:
        role = guild.get_role(rid)
        if role:
            for m in role.members:
                audience[m.id] = m
    for uid in user_ids:
        m = guild.get_member(uid)
        if m:
            audience[m.id] = m
    return role_ids, user_ids, list(audience.values())


def _option_emoji(poll: Poll, idx: int) -> str:
    if poll.is_yesno:
        return "✅" if idx == 0 else "❌"
    return NUM_EMOJI[idx + 1]


def _build_poll_embed(poll: Poll) -> discord.Embed:
    embed = discord.Embed(
        title=poll.topic,
        color=discord.Color.dark_grey() if poll.closed else discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.description = "\n".join(
        f"{_option_emoji(poll, idx)} {option}" for idx, option in enumerate(poll.options)
    )

    for idx, option in enumerate(poll.options):
        voters = poll.votes[idx]
        header = f"{_option_emoji(poll, idx)} {option} — {len(voters)} vote{'s' if len(voters) != 1 else ''}"
        if poll.anonymous:
            value = "\u200b"
        else:
            value = "\n".join(f"<@{uid}>" for uid in voters) if voters else "None"
        embed.add_field(name=header, value=value, inline=True)

    footer_bits = [
        "Multiple votes allowed" if poll.multiple_votes else "Single vote",
        "Anonymous" if poll.anonymous else "Public votes",
    ]
    if poll.closed:
        footer_bits.append("CLOSED")
    embed.set_footer(text=" • ".join(footer_bits))
    return embed


class PollButton(discord.ui.Button):
    def __init__(self, poll: Poll, idx: int):
        if poll.is_yesno:
            style = discord.ButtonStyle.success if idx == 0 else discord.ButtonStyle.danger
        else:
            style = discord.ButtonStyle.primary
        super().__init__(style=style, emoji=_option_emoji(poll, idx), custom_id=f"poll:{poll.id}:{idx}")
        self.poll_id = poll.id
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        await _handle_vote(interaction, self.poll_id, self.idx)


class PollView(discord.ui.View):
    def __init__(self, poll: Poll):
        super().__init__(timeout=None)
        for idx in range(len(poll.options)):
            self.add_item(PollButton(poll, idx))


async def _handle_vote(interaction: discord.Interaction, poll_id: str, idx: int) -> None:
    poll = _active_polls.get(poll_id)
    if poll is None:
        await interaction.response.send_message(
            "⚠️ This poll is no longer active (the bot may have restarted). Ask an organiser to open a new one.",
            ephemeral=True,
        )
        return
    if poll.closed:
        await interaction.response.send_message("This poll is closed.", ephemeral=True)
        return

    member = interaction.user
    if not isinstance(member, discord.Member) or not poll.is_eligible(member):
        await interaction.response.send_message("⛔ You are not part of the audience for this poll.", ephemeral=True)
        return

    user_id = member.id
    already_this_option = user_id in poll.votes[idx]

    if poll.multiple_votes:
        if already_this_option:
            poll.votes[idx].discard(user_id)
        else:
            poll.votes[idx].add(user_id)
    else:
        for s in poll.votes.values():
            s.discard(user_id)
        if not already_this_option:
            poll.votes[idx].add(user_id)

    await interaction.response.edit_message(embed=_build_poll_embed(poll), view=PollView(poll))


async def _close_poll(bot: commands.Bot, poll: Poll) -> None:
    poll.closed = True
    channel = bot.get_channel(poll.channel_id)
    if channel is not None and poll.message_id is not None:
        try:
            msg = await channel.fetch_message(poll.message_id)
            await msg.edit(embed=_build_poll_embed(poll), view=None)
        except Exception:
            pass
    _active_polls.pop(poll.id, None)


class ClosePollSelect(discord.ui.Select):
    def __init__(self, polls: list[Poll]):
        options = [discord.SelectOption(label=p.topic[:100], value=p.id) for p in polls[:25]]
        super().__init__(placeholder="Select a poll to close…", options=options)

    async def callback(self, interaction: discord.Interaction):
        poll = _active_polls.get(self.values[0])
        if poll is None or poll.closed:
            await interaction.response.send_message("That poll is already closed.", ephemeral=True)
            return
        await _close_poll(interaction.client, poll)
        await interaction.response.edit_message(content=f"✅ Closed **{poll.topic}**.", view=None)


class ClosePollSelectView(discord.ui.View):
    def __init__(self, polls: list[Poll]):
        super().__init__(timeout=60)
        self.add_item(ClosePollSelect(polls))


class YesNoAskView(discord.ui.View):
    """Generic Yes/No prompt used to gather creator preferences before a poll is built."""

    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("⛔ Only the poll creator can answer this.", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, value: bool):
        self.value = value
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="YES", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, True)

    @discord.ui.button(label="NO", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, False)


POLL_FILTER_MSG = "⛔ এই চ্যানেলটি শুধুমাত্র ভোটিং-এর জন্য। অন্য কোনো মেসেজ পাঠাবেন না।"
YESNO_FILTER_MSG = "⛔ এই চ্যানেলটি শুধুমাত্র Yes/No ভোটিং-এর জন্য। অন্য কোনো মেসেজ পাঠাবেন না।"


class PollsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Delete any plain text message in poll/yes-no channels and warn the sender."""
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return

        name = message.channel.name
        if name not in (POLL_CHANNEL_NAME, YESNO_CHANNEL_NAME):
            return

        # Allow only bot-posted embeds (poll cards); delete everything else
        try:
            await message.delete()
        except Exception:
            pass

        warning = POLL_FILTER_MSG if name == POLL_CHANNEL_NAME else YESNO_FILTER_MSG
        try:
            await message.channel.send(
                f"{message.author.mention} {warning}", delete_after=6
            )
        except Exception:
            pass

    @app_commands.command(name="newpoll", description="Create a poll or yes/no vote in this channel.")
    @app_commands.describe(
        topic="The poll question / title",
        options="Comma-separated options (poll channel only, max 9)",
        for_="Mention the roles/users allowed to vote (leave empty for everyone)",
    )
    @app_commands.rename(for_="for")
    async def newpoll(
        self,
        interaction: discord.Interaction,
        topic: str,
        options: str | None = None,
        for_: str | None = None,
    ):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command can only be used in a server text channel.", ephemeral=True
            )
            return
        channel = interaction.channel

        if channel.name not in (POLL_CHANNEL_NAME, YESNO_CHANNEL_NAME):
            await interaction.response.send_message(
                f"❌ `/newpoll` can only be used in **{POLL_CHANNEL_NAME}** or **{YESNO_CHANNEL_NAME}**.",
                ephemeral=True,
            )
            return

        if not _is_poll_admin(interaction.user):
            await interaction.response.send_message("⛔ Only **CAP / TABBY / ORG** may create polls.", ephemeral=True)
            return

        is_yesno = channel.name == YESNO_CHANNEL_NAME

        if is_yesno:
            if options:
                await interaction.response.send_message(
                    "⚠️ `options` is not used for yes/no votes — this uses fixed YES/NO buttons.", ephemeral=True
                )
                return
            option_list = ["YES", "NO"]
        else:
            if not options:
                await interaction.response.send_message(
                    "⚠️ `options` is required for a poll (comma-separated).", ephemeral=True
                )
                return
            option_list = [o.strip() for o in options.split(",") if o.strip()]
            if len(option_list) < 2:
                await interaction.response.send_message("⚠️ Provide at least 2 options.", ephemeral=True)
                return
            if len(option_list) > MAX_OPTIONS:
                await interaction.response.send_message(
                    f"⚠️ A poll may have at most {MAX_OPTIONS} options.", ephemeral=True
                )
                return

        role_ids, user_ids, audience = _parse_targets(guild, for_)

        if is_yesno:
            multiple_votes = False
            anonymous = True
            await interaction.response.send_message("✅ Yes/No vote created below.", ephemeral=True)
        else:
            mv_view = YesNoAskView(interaction.user.id)
            await interaction.response.send_message("**Allow multiple votes?**", view=mv_view, ephemeral=True)
            await mv_view.wait()
            if mv_view.value is None:
                await interaction.followup.send("⌛ Timed out waiting for an answer. Run `/newpoll` again.", ephemeral=True)
                return
            multiple_votes = mv_view.value

            anon_view = YesNoAskView(interaction.user.id)
            await interaction.followup.send("**Anonymous voting?**", view=anon_view, ephemeral=True)
            await anon_view.wait()
            if anon_view.value is None:
                await interaction.followup.send("⌛ Timed out waiting for an answer. Run `/newpoll` again.", ephemeral=True)
                return
            anonymous = anon_view.value
            await interaction.followup.send("✅ Poll created below.", ephemeral=True)

        poll = Poll(
            guild_id=guild.id,
            channel_id=channel.id,
            creator_id=interaction.user.id,
            topic=topic,
            options=option_list,
            multiple_votes=multiple_votes,
            anonymous=anonymous,
            is_yesno=is_yesno,
            role_ids=role_ids,
            user_ids=user_ids,
        )
        _active_polls[poll.id] = poll

        msg = await channel.send(embed=_build_poll_embed(poll), view=PollView(poll))
        poll.message_id = msg.id

        for member in audience:
            try:
                await member.send(f"📊 A poll has been opened for you in {channel.mention}. Please participate.")
            except Exception:
                pass

    @app_commands.command(name="close", description="Close an active poll or yes/no vote.")
    @app_commands.describe(where="Exact topic/title of the poll to close (leave empty to pick from a list)")
    async def close(self, interaction: discord.Interaction, where: str | None = None):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        if not _is_poll_admin(interaction.user):
            await interaction.response.send_message("⛔ Only **CAP / TABBY / ORG** may close polls.", ephemeral=True)
            return

        open_polls = _guild_open_polls(guild.id)
        if not open_polls:
            await interaction.response.send_message("ℹ️ There are no active polls right now.", ephemeral=True)
            return

        if where:
            matches = [p for p in open_polls if p.topic.lower() == where.strip().lower()]
            if not matches:
                await interaction.response.send_message(f"❌ No active poll titled **{where}** was found.", ephemeral=True)
                return
            await _close_poll(self.bot, matches[0])
            await interaction.response.send_message(f"✅ Closed **{matches[0].topic}**.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Select a poll to close:", view=ClosePollSelectView(open_polls), ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PollsCog(bot))

import discord
from discord.ext import commands
import asyncio
from config import ADMIN_ROLE_NAMES

ALLOWED_ROLES = {
    "ORG", "CAP", "TABBY",
    "INVITED ADJUDICATOR", "INDEPENDENT ADJUDICATOR",
}

PREP_KEYWORDS = {"PREP"}
DEBATE_ROOM_KEYWORD = "DEBATE ROOM"


def _is_allowed(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.name in ALLOWED_ROLES for r in member.roles)


def _in_allin_channel(channel: discord.abc.GuildChannel) -> bool:
    if not isinstance(channel, discord.TextChannel):
        return False
    return channel.name.lower() == "all-in"


def _find_debate_room(category: discord.CategoryChannel) -> discord.VoiceChannel | None:
    for ch in category.voice_channels:
        if DEBATE_ROOM_KEYWORD in ch.name.upper():
            return ch
    return None


def _find_prep_channels(category: discord.CategoryChannel) -> list[discord.VoiceChannel]:
    return [
        ch for ch in category.voice_channels
        if any(kw in ch.name.upper() for kw in PREP_KEYWORDS)
    ]


class AllInView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Push Back All",
        style=discord.ButtonStyle.danger,
        custom_id="kamla:allin:push_back",
        emoji="🔴",
    )
    async def push_back_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_allowed(interaction.user):
            await interaction.response.send_message(
                "⛔ Only **ORG / CAP / TABBY / Invited Adjudicator / Independent Adjudicator** "
                "may use the Push Back All button.",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.category is None:
            await interaction.response.send_message(
                "❌ Could not determine the room category.", ephemeral=True
            )
            return

        category = channel.category
        debate_room = _find_debate_room(category)
        if debate_room is None:
            await interaction.response.send_message(
                "❌ Could not find **DEBATE ROOM** voice channel in this room.", ephemeral=True
            )
            return

        prep_channels = _find_prep_channels(category)
        if not prep_channels:
            await interaction.response.send_message(
                "ℹ️ No prep rooms found in this room category.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        moved = 0
        failed = 0
        for prep_vc in prep_channels:
            for member in list(prep_vc.members):
                try:
                    await member.move_to(debate_room, reason="KAMLA Push Back All")
                    moved += 1
                    await asyncio.sleep(0.1)
                except discord.Forbidden:
                    failed += 1
                except Exception:
                    failed += 1

        parts = [f"✅ Moved **{moved}** member(s) back to **{debate_room.name}**."]
        if failed:
            parts.append(f"⚠️ Failed to move {failed} member(s) (no permission or not in voice).")

        await interaction.followup.send("\n".join(parts), ephemeral=True)


class AllInCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Delete any human message in #all-in channels — text is not allowed."""
        if message.author.bot or not message.guild:
            return
        if not _in_allin_channel(message.channel):
            return

        try:
            await message.delete()
        except Exception:
            pass

        try:
            await message.channel.send(
                f"⛔ {message.author.mention} — sending messages in **#all-in** "
                "is not allowed. Use the **Push Back All** button only.",
                delete_after=6,
            )
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AllInCog(bot))

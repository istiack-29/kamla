import discord
from discord import app_commands
from discord.ext import commands
from config import (
    KAMLA_ROLE_NAMES, ADMIN_ROLE_NAMES,
    get_kamla_role, assign_kamla_role,
)

ASSIGN_CHANNEL_ROLE_MAP: dict[str, str] = {
    "org":                      "ORG",
    "cap":                      "CAP",
    "tabby":                    "TABBY",
    "equity":                   "EQUITY",
    "debater":                  "DEBATER",
    "visitor":                  "VISITOR",
    "independent-adjudicator":  "INDEPENDENT ADJUDICATOR",
    "invited-adjudicator":      "INVITED ADJUDICATOR",
}


def _channel_role(channel_name: str) -> str | None:
    return ASSIGN_CHANNEL_ROLE_MAP.get(channel_name.lower())


def _in_assign_category(channel: discord.TextChannel) -> bool:
    if channel.category is None:
        return False
    return "assign" in channel.category.name.lower()


class AssignCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if not _in_assign_category(message.channel):
            return

        if message.author.bot:
            # Bot-originated messages are trusted (sent by role buttons or admin automation)
            pass
        else:
            member = message.author
            is_admin = (
                member.guild_permissions.administrator
                or any(r.name in ADMIN_ROLE_NAMES for r in member.roles)
            )
            if not is_admin:
                try:
                    await message.delete()
                except Exception:
                    pass
                return

        role_name = _channel_role(message.channel.name)
        if not role_name:
            return

        role = get_kamla_role(message.guild, role_name)
        if role is None:
            if not message.author.bot:
                await message.channel.send(
                    f"❌ Role **{role_name}** not found. Ensure the server was set up with `/st`.",
                    delete_after=10,
                )
            return

        targets = [m for m in message.mentions if not m.bot]
        if not targets:
            return

        results = []
        for target in targets:
            await assign_kamla_role(target, role)
            results.append(target.mention)

        # Confirmation only for human-sent messages to avoid bot loop
        if results and not message.author.bot:
            await message.channel.send(
                f"✅ Assigned **{role_name}** to: {', '.join(results)}",
                delete_after=15,
            )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        assign_cat = discord.utils.get(guild.categories, name="🛅︱ASSIGN")
        if assign_cat is None:
            return
        for ch in assign_cat.text_channels:
            try:
                async for msg in ch.history(limit=200):
                    if member in msg.mentions:
                        try:
                            await msg.delete()
                        except Exception:
                            pass
            except Exception:
                pass


    @app_commands.command(name="mention", description="Mention a user in a channel.")
    @app_commands.describe(user="User to mention", channel="Channel to mention them in")
    @app_commands.default_permissions(administrator=True)
    async def mention(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        channel: discord.TextChannel,
    ):
        try:
            await channel.send(user.mention)
            await interaction.response.send_message(
                f"✅ Mentioned {user.mention} in {channel.mention}.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ Cannot send messages in {channel.mention}.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AssignCog(bot))

import discord
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
        if message.author.bot:
            return
        if not message.guild:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if not _in_assign_category(message.channel):
            return

        member = message.author
        sender_is_admin = (
            member.guild_permissions.administrator
            or any(r.name in ADMIN_ROLE_NAMES for r in member.roles)
        )
        if not sender_is_admin:
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
            await message.channel.send(
                f"❌ Role **{role_name}** not found. Run `/st` first.", delete_after=10
            )
            return

        targets: list[discord.Member] = list(message.mentions)
        if not targets:
            return

        results = []
        for target in targets:
            if target.bot:
                continue
            await assign_kamla_role(target, role)
            results.append(target.mention)

        if results:
            await message.channel.send(
                f"✅ Assigned **{role_name}** to: {', '.join(results)}",
                delete_after=15,
            )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """No-op placeholder — member-leave cleanup handled in main.py."""
        pass

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """If an admin manually changes a role, clean stale assign-channel mentions."""
        pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AssignCog(bot))

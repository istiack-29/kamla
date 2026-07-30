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

# Reverse map: role name → assign channel name
ROLE_ASSIGN_CHANNEL_MAP: dict[str, str] = {v: k for k, v in ASSIGN_CHANNEL_ROLE_MAP.items()}

# Track roles the bot itself just assigned so on_member_update doesn't double-post.
# Key: (guild_id, member_id, role_name) — consumed once when on_member_update fires.
_bot_assigned_roles: set[tuple[int, int, str]] = set()


def _channel_role(channel_name: str) -> str | None:
    return ASSIGN_CHANNEL_ROLE_MAP.get(channel_name.lower())


def _in_assign_category(channel: discord.TextChannel) -> bool:
    if channel.category is None:
        return False
    return "assign" in channel.category.name.lower()


async def _cleanup_old_mentions(
    assign_cat: discord.CategoryChannel,
    member: discord.Member,
    keep_channel_id: int,
) -> None:
    """Delete any messages mentioning `member` in OTHER assign channels."""
    for ch in assign_cat.text_channels:
        if ch.id == keep_channel_id:
            continue
        try:
            async for msg in ch.history(limit=200):
                if member in msg.mentions:
                    try:
                        await msg.delete()
                    except Exception:
                        pass
        except Exception:
            pass


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

        # Only mentions allowed — any other message gets deleted
        # (for both humans and unexpected bot text).
        if message.author.bot:
            # Trust bot mention messages; delete bot messages with no mentions.
            if not message.mentions:
                # Keep confirmation messages (they self-delete via delete_after).
                # Only delete bot messages that aren't mentions and aren't replies.
                pass
        else:
            member = message.author
            is_admin = (
                member.guild_permissions.administrator
                or any(r.name in ADMIN_ROLE_NAMES for r in member.roles)
            )

            # Non-admin → delete instantly
            if not is_admin:
                try:
                    await message.delete()
                except Exception:
                    pass
                return

            # Admin but the message contains NO user mention → not allowed
            human_mentions = [m for m in message.mentions if not m.bot]
            if not human_mentions:
                try:
                    await message.delete()
                except Exception:
                    pass
                try:
                    await message.channel.send(
                        f"⚠️ {member.mention} — only **mentions** are allowed in assign channels. "
                        "Mention a user (e.g. `@username`) to assign them the role.",
                        delete_after=8,
                    )
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
        assign_cat = message.channel.category
        for target in targets:
            # Mark as bot-assigned so on_member_update skips double-post
            _bot_assigned_roles.add((message.guild.id, target.id, role_name))
            await assign_kamla_role(target, role)
            results.append(target.mention)
            # Remove the user's previous mention from any other assign channel
            if assign_cat is not None:
                await _cleanup_old_mentions(assign_cat, target, message.channel.id)

        if results and not message.author.bot:
            await message.channel.send(
                f"✅ Assigned **{role_name}** to: {', '.join(results)}",
                delete_after=15,
            )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """
        Detect when a KAMLA role is added to a member by ANY means (manual, another bot, etc.)
        and post/update the mention in the corresponding assign channel.
        """
        guild = after.guild

        before_role_names = {r.name for r in before.roles}
        after_role_names  = {r.name for r in after.roles}

        added_kamla = [
            name for name in (after_role_names - before_role_names)
            if name in KAMLA_ROLE_NAMES
        ]
        if not added_kamla:
            return

        assign_cat = discord.utils.get(guild.categories, name="🛅︱ASSIGN")
        if assign_cat is None:
            return

        for role_name in added_kamla:
            key = (guild.id, after.id, role_name)

            # If it was the bot's own assignment (via on_message), skip to avoid double-post
            if key in _bot_assigned_roles:
                _bot_assigned_roles.discard(key)
                continue

            ch_name = ROLE_ASSIGN_CHANNEL_MAP.get(role_name)
            if not ch_name:
                continue

            assign_ch = discord.utils.get(assign_cat.text_channels, name=ch_name)
            if assign_ch is None:
                continue

            # Clean up old mentions in all other assign channels first
            await _cleanup_old_mentions(assign_cat, after, assign_ch.id)

            # Post the mention in the correct channel
            try:
                await assign_ch.send(after.mention)
            except Exception as e:
                print(f"[AssignCog] Could not post mention in #{ch_name}: {e}")

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

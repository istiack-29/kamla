import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv
from keep_alive import keep_alive
from config import config_manager

load_dotenv()

INTENTS = discord.Intents.all()

COGS = [
    "setup_cog",
    "settings_cog",
    "roles_cog",
    "assign_cog",
    "timer_cog",
    "poi_cog",
    "allin_cog",
]


class KamlaBot(commands.Bot):
    def __init__(self) -> None:
        app_id_raw = os.getenv("APPLICATION_ID", "")
        app_id = int(app_id_raw) if app_id_raw.strip().isdigit() else None
        super().__init__(
            command_prefix="!kamla ",
            intents=INTENTS,
            application_id=app_id,
            help_command=None,
        )

    async def setup_hook(self) -> None:
        for cog in COGS:
            try:
                await self.load_extension(cog)
                print(f"[KAMLA] Loaded cog: {cog}")
            except Exception as e:
                print(f"[KAMLA] Failed to load cog {cog}: {e}")

        from roles_cog import RoleSelectView
        from allin_cog import AllInView
        from settings_cog import SettingsView
        from setup_cog import OnJoinView

        self.add_view(RoleSelectView())
        self.add_view(AllInView())
        self.add_view(SettingsView())
        self.add_view(OnJoinView(installer_id=0))

        try:
            synced = await self.tree.sync()
            print(f"[KAMLA] Synced {len(synced)} slash command(s).")
        except Exception as e:
            print(f"[KAMLA] Command sync failed: {e}")

    async def on_ready(self) -> None:
        print(f"[KAMLA] Ready — logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="tournament servers 🏆",
            ),
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        print(f"[KAMLA] Joined guild: {guild.name} ({guild.id})")
        await config_manager.ensure_config_channel(guild)

        installer: discord.Member | None = None
        try:
            await asyncio.sleep(1)
            async for entry in guild.audit_logs(
                action=discord.AuditLogAction.bot_add, limit=5
            ):
                if entry.target and entry.target.id == self.user.id:
                    installer = entry.user
                    break
        except discord.Forbidden:
            pass

        from webhook import send_join_log
        await send_join_log(guild, installer)

        channel = guild.system_channel
        if channel is None:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    channel = ch
                    break

        if channel is None:
            return

        from setup_cog import OnJoinView
        embed = discord.Embed(
            title="🎉 KAMLA has arrived!",
            description=(
                f"Hello {installer.mention if installer else 'there'}! "
                f"I'm **KAMLA** — your automated tournament server manager.\n\n"
                "Click **CREATE NOW** to build your tournament server instantly.\n"
                "Only the person who added me can use this button."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="KAMLA • Tournament Automation Bot")
        await channel.send(embed=embed, view=OnJoinView(installer_id=installer.id if installer else 0))

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if "kamla-config" in channel.name:
            guild = channel.guild
            cached = config_manager.get_cached(guild.id)
            config_manager.invalidate(guild.id)
            config_manager._cache[guild.id] = cached
            await asyncio.sleep(1)
            ch = await config_manager.ensure_config_channel(guild)
            await ch.send(
                "⚠️ **Config channel was deleted and has been recreated.** "
                "All settings have been restored from memory.\n"
                "**DO NOT DELETE THIS CHANNEL.**"
            )

    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        assign_cat = discord.utils.get(guild.categories, name="🛅︱ASSIGN")
        if assign_cat is None:
            return
        for ch in assign_cat.text_channels:
            try:
                async for msg in ch.history(limit=200):
                    if msg.author.bot:
                        continue
                    if member in msg.mentions:
                        await msg.delete()
            except Exception:
                pass


bot = KamlaBot()

if __name__ == "__main__":
    keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
    bot.run(token, reconnect=True, log_handler=None)

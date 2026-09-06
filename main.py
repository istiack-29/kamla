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
    "dm_notify_cog",
    "timer_cog",
    "poi_cog",
    "allin_cog",
    "games_cog",
    "toss_cog",
    "polls_cog",
    "welcome_cog",
]


class KamlaBot(commands.Bot):
    def __init__(self) -> None:
        app_id_raw = os.getenv("APPLICATION_ID", "")
        app_id = int(app_id_raw) if app_id_raw.strip().isdigit() else None
        self._control_owner_id: int | None = None
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

        from roles_cog    import RoleSelectView
        from allin_cog    import AllInView
        from settings_cog import SettingsView
        from setup_cog    import OnJoinView

        self.add_view(RoleSelectView())
        self.add_view(AllInView())
        self.add_view(SettingsView())
        self.add_view(OnJoinView(installer_id=0))
        # NOTE: games_cog / polls_cog views are intentionally NOT registered here.
        # Their state (active matches, active polls) is memory-only by design, so
        # a restart loses it along with the match/poll; buttons left over from a
        # previous process will simply show "This interaction failed" — expected.

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

    async def _get_control_owner_id(self) -> int | None:
        if self._control_owner_id is not None:
            return self._control_owner_id
        try:
            app_info = await self.application_info()
            self._control_owner_id = app_info.owner.id
        except Exception:
            return None
        return self._control_owner_id

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Handle owner-only Approve/Reject buttons sent through the webhook."""
        data = interaction.data or {}
        custom_id = data.get("custom_id") if isinstance(data, dict) else None
        if not isinstance(custom_id, str) or not custom_id.startswith("kamla:approval:"):
            await super().on_interaction(interaction)
            return

        parts = custom_id.split(":")
        if len(parts) != 4 or parts[2] not in {"approve", "reject"}:
            await interaction.response.send_message(
                "❌ Invalid approval action.", ephemeral=True
            )
            return

        owner_id = await self._get_control_owner_id()
        if owner_id is None or interaction.user.id != owner_id:
            await interaction.response.send_message(
                "⛔ Only the KAMLA owner can approve or reject server setup.",
                ephemeral=True,
            )
            return

        try:
            guild_id = int(parts[3])
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid server reference.", ephemeral=True
            )
            return

        guild = self.get_guild(guild_id)
        if guild is None:
            await interaction.response.send_message(
                "❌ KAMLA is no longer in that server.", ephemeral=True
            )
            return

        cfg = await config_manager.get_config(guild)
        if cfg.get("onboarding_status") != "waiting_approval":
            await interaction.response.send_message(
                "ℹ️ This setup request is no longer pending.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        requested_by = int(cfg.get("requested_by", 0))
        requester = guild.get_member(requested_by)
        if requester is None and requested_by:
            try:
                requester = await guild.fetch_member(requested_by)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                requester = None

        if parts[2] == "reject":
            cfg = await config_manager.update_config(
                guild,
                onboarding_status="rejected",
                rejected_by=interaction.user.id,
                rejected_at=discord.utils.utcnow().isoformat(),
            )
            await self._notify_setup_result(
                requester,
                "❌ KAMLA setup rejected",
                f"Your KAMLA setup request for **{guild.name}** was rejected. "
                "KAMLA will now leave the server.",
                discord.Color.red(),
            )
            from webhook import edit_join_log
            await edit_join_log(guild, cfg)
            await interaction.followup.send(
                f"❌ Rejected **{guild.name}**. KAMLA is leaving the server.",
                ephemeral=True,
            )
            await guild.leave()
            return

        cfg = await config_manager.update_config(
            guild,
            onboarding_status="building",
            approved_by=interaction.user.id,
            approved_at=discord.utils.utcnow().isoformat(),
        )
        from webhook import edit_join_log
        await edit_join_log(guild, cfg)
        await self._notify_setup_result(
            requester,
            "✅ KAMLA setup approved",
            f"Your KAMLA setup request for **{guild.name}** was approved. "
            "The tournament server is now being built.",
            discord.Color.green(),
        )
        await interaction.followup.send(
            f"✅ Approved **{guild.name}**. KAMLA is building the server now.",
            ephemeral=True,
        )

        from setup_cog import _build_approved_server
        setup_data = {
            "format": cfg.get("requested_format", "ap"),
            "rooms": int(cfg.get("requested_rooms", 1)),
            "timezone": cfg.get("requested_timezone", "+06:00"),
            "tournament_name": cfg.get("requested_tournament", "Tournament"),
            "role_name": cfg.get("requested_role", "ORG"),
            "creator_id": requested_by,
            "creator": requester,
        }
        try:
            await _build_approved_server(guild, setup_data)
            cfg = await config_manager.get_config(guild)
            await self._notify_setup_result(
                requester,
                "🎉 Your server is ready",
                f"**{guild.name}** has been approved and fully set up by KAMLA.",
                discord.Color.green(),
            )
            await edit_join_log(guild, cfg)
        except Exception as error:
            cfg = await config_manager.update_config(
                guild,
                onboarding_status="build_failed",
                build_error=str(error)[:500],
            )
            await edit_join_log(guild, cfg)
            await self._notify_setup_result(
                requester,
                "⚠️ KAMLA setup failed",
                f"The request for **{guild.name}** was approved, but the server "
                "could not be built. Please contact the KAMLA owner.",
                discord.Color.orange(),
            )

    @staticmethod
    async def _notify_setup_result(
        member: discord.Member | discord.User | None,
        title: str,
        description: str,
        color: discord.Color,
    ) -> None:
        if member is None:
            return
        try:
            await member.send(
                embed=discord.Embed(
                    title=title,
                    description=description,
                    color=color,
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

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

        existing_cfg = await config_manager.get_config(guild)
        already_approved = existing_cfg.get("onboarding_status") == "approved"
        if not already_approved:
            await config_manager.set_config(
                guild,
                {
                    "onboarding_status": "awaiting_image",
                    "installer_id": installer.id if installer else 0,
                    "server_icon_uploaded": False,
                    "locked": False,
                },
            )

        from webhook import send_join_log
        join_message_id = await send_join_log(guild, installer)
        if join_message_id:
            await config_manager.update_config(
                guild, join_webhook_message_id=join_message_id
            )

        if already_approved:
            from webhook import edit_join_log
            cfg = await config_manager.get_config(guild)
            await edit_join_log(guild, cfg)
            return

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
                "I'm **KAMLA** — your automated tournament server manager.\n\n"
                "Please upload the server profile picture as an attachment in this "
                "channel, then click **READY YOUR SERVER**.\n"
                "Only the person who added me can use this button."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="KAMLA • Tournament Automation Bot")
        onboarding_message = await channel.send(
            embed=embed,
            view=OnJoinView(installer_id=installer.id if installer else 0),
        )
        await config_manager.update_config(
            guild,
            onboarding_channel_id=channel.id,
            onboarding_message_id=onboarding_message.id,
        )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        from webhook import mark_guild_deleted
        asyncio.create_task(mark_guild_deleted(guild))

    async def on_member_join(self, member: discord.Member) -> None:
        from webhook import edit_join_log
        cfg = config_manager.get_cached(member.guild.id) or {}
        asyncio.create_task(edit_join_log(member.guild, cfg))

        # Auto-assign ORG role to the bot owner when they join
        owner_id_raw = os.getenv("OWNER_ID", "").strip()
        if owner_id_raw.isdigit() and int(owner_id_raw) == member.id:
            org_role = discord.utils.get(member.guild.roles, name="ORG")
            if org_role:
                try:
                    await member.add_roles(org_role, reason="KAMLA — bot owner auto-ORG")
                    print(f"[KAMLA] Auto-assigned ORG to bot owner in {member.guild.name}")
                except Exception as e:
                    print(f"[KAMLA] Could not auto-assign ORG to owner: {e}")

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild

        if "kamla-config" in channel.name:
            return

        # Never interfere while KAMLA is building / wiping
        from server_builder import _building_guilds, _restoring_guilds
        if guild.id in _building_guilds or guild.id in _restoring_guilds:
            return

        cfg = config_manager.get_cached(guild.id)
        if not cfg:
            try:
                cfg = await asyncio.wait_for(config_manager.get_config(guild), timeout=3.0)
            except Exception:
                return

        if not cfg.get("locked", False):
            return

        # Check audit log — if KAMLA itself created the channel, allow it
        try:
            async for entry in guild.audit_logs(
                action=discord.AuditLogAction.channel_create, limit=5
            ):
                if entry.target and entry.target.id == channel.id:
                    if entry.user and entry.user.id == self.user.id:
                        return  # KAMLA created it — skip lock enforcement
                    break
        except discord.Forbidden:
            pass

        try:
            await channel.delete(reason="🔒 KAMLA — Server is locked. Channel auto-removed.")
        except Exception:
            pass

        try:
            settings_ch = discord.utils.get(guild.text_channels, name="⚙️︱settings")
            if settings_ch and settings_ch.permissions_for(guild.me).send_messages:
                await settings_ch.send(
                    f"🔒 A new channel **#{channel.name}** was automatically deleted "
                    "because the server is locked.",
                    delete_after=30,
                )
        except Exception:
            pass

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild

        # ── Config channel guard (existing behaviour) ────────────────────────
        if "kamla-config" in channel.name:
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
            return

        # ── Protected channel/category guard ────────────────────────────────
        from server_builder import restore_if_protected, _building_guilds
        if guild.id in _building_guilds:
            return  # ignore deletions during server-build / wipe

        is_category = isinstance(channel, discord.CategoryChannel)
        parent = None
        if not is_category:
            category = getattr(channel, "category", None)
            parent = category.name if category else None
        asyncio.create_task(
            restore_if_protected(
                guild, channel.name, is_category, parent_category_name=parent
            )
        )

    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild

        # Update live member count in webhook embed
        from webhook import edit_join_log
        cfg = config_manager.get_cached(guild.id) or {}
        asyncio.create_task(edit_join_log(guild, cfg))

        # Clean up assign messages
        assign_cat = discord.utils.get(guild.categories, name="🛅︱ASSIGN")
        if assign_cat is None:
            return
        for ch in assign_cat.text_channels:
            try:
                async for msg in ch.history(limit=200):
                    if msg.author.bot and member in msg.mentions:
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

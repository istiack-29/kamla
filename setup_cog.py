import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from config import config_manager, ADMIN_ROLE_NAMES
from webhook import edit_join_log

ADMIN_ASSIGN_CHANNEL_MAP = {
    "ORG":    "org",
    "CAP":    "cap",
    "TABBY":  "tabby",
    "EQUITY": "equity",
}


class OnJoinView(discord.ui.View):
    def __init__(self, installer_id: int = 0):
        super().__init__(timeout=None)
        self.installer_id = installer_id

    @discord.ui.button(
        label="READY YOUR SERVER",
        style=discord.ButtonStyle.success,
        custom_id="kamla:on_join:create_now",
        emoji="⚡",
    )
    async def create_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This button can only be used inside a server.", ephemeral=True
            )
            return

        cfg = await config_manager.get_config(interaction.guild)
        installer_id = int(cfg.get("installer_id", self.installer_id or 0))
        if not installer_id or interaction.user.id != installer_id:
            await interaction.response.send_message(
                "⛔ Only the person who added KAMLA to this server may use this button.",
                ephemeral=True,
            )
            return

        if cfg.get("onboarding_status") == "waiting_approval":
            await interaction.response.send_message(
                "🕐 Your setup request is already waiting for owner approval.",
                ephemeral=True,
            )
            return

        if not cfg.get("server_icon_uploaded"):
            await interaction.response.send_message(
                "🖼️ Please upload the server profile picture first, then click this button.",
                ephemeral=True,
            )
            return

        cfg = await config_manager.update_config(
            interaction.guild,
            onboarding_status="ready_to_configure",
        )
        asyncio.create_task(edit_join_log(interaction.guild, cfg))
        await interaction.response.send_message(
            "✅ Your server is ready for configuration.\n\n"
            "Use this command to request setup:\n"
            "```\n/st format:ap room:20 tournamentname:MY OPEN 2026 as:ORG\n```\n"
            "After you submit it, the KAMLA owner must approve the request.",
            ephemeral=True,
        )


class ConfirmBuildView(discord.ui.View):
    def __init__(self, setup_data: dict):
        super().__init__(timeout=180)
        self.setup_data = setup_data

    @discord.ui.button(label="Confirm & Build", style=discord.ButtonStyle.success, emoji="🚀")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.defer(thinking=True, ephemeral=True)
        await _do_build(interaction, self.setup_data)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            content="❌ Setup cancelled.", embed=None, view=None
        )


async def _build_approved_server(guild: discord.Guild, setup_data: dict) -> None:
    """Build a server only after the KAMLA owner has approved its request."""
    from server_builder import wipe_server, build_server

    await wipe_server(guild)
    await build_server(
        guild=guild,
        fmt=setup_data["format"],
        rooms=setup_data["rooms"],
        timezone=setup_data["timezone"],
        tournament_name=setup_data["tournament_name"],
        creator_id=setup_data["creator_id"],
    )
    creator = setup_data.get("creator")
    if creator is not None:
        await _assign_creator_role(guild, creator, setup_data["role_name"])

    cfg = await config_manager.get_config(guild)
    await edit_join_log(guild, cfg)


async def submit_setup_for_approval(
    interaction: discord.Interaction,
    setup_data: dict,
) -> None:
    """Save a pending request and update the owner's webhook approval card."""
    guild = interaction.guild
    if guild is None:
        return

    cfg = await config_manager.update_config(
        guild,
        onboarding_status="waiting_approval",
        requested_by=interaction.user.id,
        requested_username=interaction.user.name,
        requested_tournament=setup_data["tournament_name"],
        requested_role=setup_data["role_name"],
        requested_format=setup_data["format"],
        requested_rooms=setup_data["rooms"],
        requested_timezone=setup_data["timezone"],
        approval_requested_at=discord.utils.utcnow().isoformat(),
    )
    await edit_join_log(guild, cfg)
    await interaction.response.send_message(
        "🕐 **Waiting for approval.**\n"
        "The KAMLA owner has received your request. "
        "You will receive a DM when it is approved or rejected.",
        ephemeral=True,
    )


async def _do_build(interaction: discord.Interaction, setup_data: dict) -> None:
    guild = interaction.guild
    fmt            = setup_data["format"]
    rooms          = setup_data["rooms"]
    tz             = setup_data["timezone"]
    tournament_name = setup_data["tournament_name"]
    creator_id     = interaction.user.id
    role_name      = setup_data["role_name"]

    status_msg = await interaction.followup.send(
        embed=discord.Embed(
            title="⚙️ Building Tournament Server…",
            description=(
                "Please wait. This may take 1–3 minutes depending on room count.\n\n"
                "1. 🧹 Wiping old channels and roles…\n"
                "2. 🏗️ Creating structure…\n"
                "3. 🚀 Finalising…"
            ),
            color=discord.Color.yellow(),
        ),
        ephemeral=True,
        wait=True,
    )

    try:
        await _build_approved_server(
            guild,
            {
                "format": fmt,
                "rooms": rooms,
                "timezone": tz,
                "tournament_name": tournament_name,
                "creator_id": creator_id,
                "creator": interaction.user,
                "role_name": role_name,
            },
        )

        # ── Permanent invite ──────────────────────────────────────────────────
        invite_url = ""
        try:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).create_instant_invite:
                    inv = await ch.create_invite(max_age=0, max_uses=0, reason="KAMLA post-build")
                    invite_url = inv.url
                    break
        except Exception:
            pass

        # ── Edit webhook log ──────────────────────────────────────────────────
        cfg = await config_manager.get_config(guild)
        await edit_join_log(guild, cfg)

        # ── Completion report ─────────────────────────────────────────────────
        embed = discord.Embed(title="✅ Tournament Server Ready!", color=discord.Color.green())
        embed.add_field(name="Tournament",  value=tournament_name,  inline=True)
        embed.add_field(name="Format",      value=fmt.upper(),      inline=True)
        embed.add_field(name="Rooms",       value=str(rooms),       inline=True)
        embed.add_field(name="Timezone",    value=tz,               inline=True)
        embed.add_field(name="Your Role",   value=role_name.upper(), inline=True)
        if invite_url:
            embed.add_field(name="Server Link", value=f"[Join]({invite_url})", inline=True)
        embed.set_footer(text="KAMLA • Tournament Automation Bot")

        try:
            await status_msg.edit(embed=embed)
        except Exception:
            await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        err_embed = discord.Embed(
            title="❌ Build Failed",
            description=(
                f"An error occurred:\n```{e}```\n"
                "Ensure KAMLA has **Administrator** permission and try again."
            ),
            color=discord.Color.red(),
        )
        try:
            await status_msg.edit(embed=err_embed)
        except Exception:
            await interaction.followup.send(embed=err_embed, ephemeral=True)
        raise


async def _assign_creator_role(guild: discord.Guild, user: discord.Member, role_name: str) -> None:
    """Route creator role assignment through the assign channel (single source of truth)."""
    assign_cat = discord.utils.get(guild.categories, name="🛅︱ASSIGN")
    if assign_cat is None:
        return

    ch_name = ADMIN_ASSIGN_CHANNEL_MAP.get(role_name.upper(), role_name.lower().replace(" ", "-"))
    assign_ch = discord.utils.get(assign_cat.text_channels, name=ch_name)

    if assign_ch:
        try:
            await assign_ch.send(user.mention)
        except Exception:
            pass
    else:
        # Fallback: direct assignment if channel not found
        from config import get_kamla_role, assign_kamla_role
        role = get_kamla_role(guild, role_name.upper())
        if role:
            await assign_kamla_role(user, role)


FORMAT_CHOICES = [
    app_commands.Choice(name="AP (Asian Parliamentary)", value="ap"),
    app_commands.Choice(name="BP (British Parliamentary)", value="bp"),
]

ROLE_CHOICES = [
    app_commands.Choice(name="ORG",    value="ORG"),
    app_commands.Choice(name="CAP",    value="CAP"),
    app_commands.Choice(name="TABBY",  value="TABBY"),
    app_commands.Choice(name="EQUITY", value="EQUITY"),
]


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Accept only the installer’s image upload during onboarding."""
        if not message.guild or message.author.bot or not message.attachments:
            return

        cfg = await config_manager.get_config(message.guild)
        status = cfg.get("onboarding_status")
        if status not in {"awaiting_image", "image_uploaded", "ready_to_configure"}:
            return

        if message.author.id != int(cfg.get("installer_id", 0)):
            return
        if message.channel.id != int(cfg.get("onboarding_channel_id", 0)):
            return

        attachment = next(
            (
                item for item in message.attachments
                if (item.content_type or "").startswith("image/")
                or item.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
            ),
            None,
        )
        if attachment is None:
            return
        if attachment.size > 10 * 1024 * 1024:
            await message.channel.send(
                "❌ This image is too large. Please upload an image under 10 MB.",
                delete_after=10,
            )
            return

        try:
            image_bytes = await attachment.read()
            await message.guild.edit(
                icon=image_bytes,
                reason="KAMLA onboarding — server profile picture",
            )
        except (discord.Forbidden, discord.HTTPException):
            await message.channel.send(
                "❌ KAMLA could not update the server picture. "
                "Please ensure it has **Manage Server** permission.",
                delete_after=12,
            )
            return

        cfg = await config_manager.update_config(
            message.guild,
            onboarding_status="image_uploaded",
            server_icon_uploaded=True,
            server_icon_filename=attachment.filename,
        )
        await edit_join_log(message.guild, cfg)
        await message.channel.send(
            "✅ Server profile picture uploaded successfully. "
            "Now click **READY YOUR SERVER** on the KAMLA setup message.",
            delete_after=15,
        )

    @app_commands.command(name="st", description="Set up the KAMLA tournament server.")
    @app_commands.describe(
        format="Debate format",
        room="Number of debate rooms (1–1000)",
        tournamentname="Full name of the tournament",
        as_role="Your role in this tournament",
    )
    @app_commands.choices(format=FORMAT_CHOICES, as_role=ROLE_CHOICES)
    @app_commands.rename(as_role="as")
    @app_commands.default_permissions(administrator=True)
    async def st(
        self,
        interaction: discord.Interaction,
        format: app_commands.Choice[str],
        room: app_commands.Range[int, 1, 1000],
        tournamentname: str,
        as_role: app_commands.Choice[str],
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return

        if not interaction.user.guild_permissions.administrator:
            has_admin_role = any(r.name in ADMIN_ROLE_NAMES for r in interaction.user.roles)
            if not has_admin_role:
                await interaction.response.send_message(
                    "⛔ You need an admin role (ORG/CAP/TABBY/EQUITY) or Administrator permission.",
                    ephemeral=True,
                )
                return

        cfg = await config_manager.get_config(interaction.guild)
        onboarding_status = cfg.get("onboarding_status", "approved")
        if onboarding_status != "approved":
            installer_id = int(cfg.get("installer_id", 0))
            if interaction.user.id != installer_id:
                await interaction.response.send_message(
                    "⛔ This server is waiting for setup approval from the person "
                    "who added KAMLA.",
                    ephemeral=True,
                )
                return

            if onboarding_status in {"awaiting_image", "image_uploaded"}:
                await interaction.response.send_message(
                    "🖼️ First upload the server profile picture and click "
                    "**READY YOUR SERVER**.",
                    ephemeral=True,
                )
                return

            if onboarding_status == "waiting_approval":
                await interaction.response.send_message(
                    "🕐 A setup request is already waiting for KAMLA owner approval.",
                    ephemeral=True,
                )
                return

            if onboarding_status == "rejected":
                await interaction.response.send_message(
                    "⛔ This server was rejected and cannot use KAMLA.",
                    ephemeral=True,
                )
                return

            pending_setup = {
                "format": format.value,
                "rooms": room,
                "timezone": "+06:00",
                "tournament_name": tournamentname,
                "role_name": as_role.value,
            }
            await submit_setup_for_approval(interaction, pending_setup)
            return

        setup_data = {
            "format":          format.value,
            "rooms":           room,
            "timezone":        "+06:00",
            "tournament_name": tournamentname,
            "role_name":       as_role.value,
        }

        embed = discord.Embed(
            title="🏆 Tournament Setup — Confirm",
            description=(
                f"**Tournament:** {tournamentname}\n"
                f"**Format:** {format.name}\n"
                f"**Rooms:** {room}\n"
                f"**Your Role:** {as_role.value}\n\n"
                "⚠️ This will **wipe all existing channels and roles** and rebuild the server.\n"
                "Press **Confirm & Build** to proceed."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed, view=ConfirmBuildView(setup_data), ephemeral=True
        )

    @app_commands.command(name="rebuild", description="Rebuild the tournament server structure.")
    @app_commands.default_permissions(administrator=True)
    async def rebuild(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return
        cfg = await config_manager.get_config(interaction.guild)
        if not cfg:
            await interaction.response.send_message(
                "⛔ No tournament config found. Use `/st` first.", ephemeral=True
            )
            return
        if cfg.get("onboarding_status", "approved") != "approved":
            await interaction.response.send_message(
                "⛔ This server has not been approved by the KAMLA owner yet.",
                ephemeral=True,
            )
            return
        setup_data = {
            "format":          cfg.get("format", "ap"),
            "rooms":           cfg.get("rooms", 1),
            "timezone":        cfg.get("timezone", "+06:00"),
            "tournament_name": cfg.get("tournament_name", "Tournament"),
            "role_name":       "ORG",
        }
        embed = discord.Embed(
            title="🔨 Rebuild Server",
            description=(
                "⚠️ This will **wipe and rebuild** the entire server from saved configuration.\n\n"
                "Press **Confirm & Build** to proceed."
            ),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(
            embed=embed, view=ConfirmBuildView(setup_data), ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

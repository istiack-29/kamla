import discord
from discord import app_commands
from discord.ext import commands
import asyncio
from config import config_manager, ADMIN_ROLE_NAMES


class OnJoinView(discord.ui.View):
    def __init__(self, installer_id: int = 0):
        super().__init__(timeout=None)
        self.installer_id = installer_id

    @discord.ui.button(
        label="CREATE NOW",
        style=discord.ButtonStyle.success,
        custom_id="kamla:on_join:create_now",
        emoji="⚡",
    )
    async def create_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.installer_id and interaction.user.id != self.installer_id:
            await interaction.response.send_message(
                "⛔ Only the person who added KAMLA to this server may use this button.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "🚀 To quickly set up your tournament server, use the slash command:\n"
            "```\n/st format:ap room:20 tournamentname:MY OPEN 2026 as:ORG\n```\n"
            "Replace the values with your tournament details.",
            ephemeral=True,
        )


class LogoUploadModal(discord.ui.Modal, title="Upload Tournament Logo URL"):
    logo_url = discord.ui.TextInput(
        label="Image URL (must end in .png/.jpg/.gif)",
        placeholder="https://example.com/logo.png",
        required=True,
        max_length=512,
    )

    def __init__(self, setup_data: dict):
        super().__init__()
        self.setup_data = setup_data

    async def on_submit(self, interaction: discord.Interaction):
        url = self.logo_url.value.strip()
        await interaction.response.defer(thinking=True, ephemeral=True)
        await _do_build(interaction, self.setup_data, logo_url=url)


class LogoChoiceView(discord.ui.View):
    def __init__(self, setup_data: dict):
        super().__init__(timeout=300)
        self.setup_data = setup_data

    @discord.ui.button(label="Upload Logo", style=discord.ButtonStyle.primary, emoji="🖼️")
    async def upload_logo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LogoUploadModal(self.setup_data))
        self.stop()

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        await _do_build(interaction, self.setup_data, logo_url=None)
        self.stop()


async def _do_build(interaction: discord.Interaction, setup_data: dict, logo_url: str | None) -> None:
    guild = interaction.guild
    fmt = setup_data["format"]
    rooms = setup_data["rooms"]
    timezone = setup_data["timezone"]
    tournament_name = setup_data["tournament_name"]
    creator_id = interaction.user.id
    role_name = setup_data["role_name"]

    status_msg = await interaction.followup.send(
        embed=discord.Embed(
            title="⚙️ Building Tournament Server…",
            description=(
                "Please wait. This may take 1–3 minutes depending on the number of rooms.\n\n"
                "**Steps:**\n"
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
        from server_builder import wipe_server, build_server
        await wipe_server(guild)
        await build_server(
            guild=guild,
            fmt=fmt,
            rooms=rooms,
            timezone=timezone,
            tournament_name=tournament_name,
            creator_id=creator_id,
            logo_url=logo_url,
        )

        # Assign the requested role to the creator
        role = discord.utils.get(guild.roles, name=role_name.upper())
        if role:
            try:
                await interaction.user.add_roles(role, reason="KAMLA setup — creator role assignment")
            except Exception:
                pass

        # Generate permanent invite
        invite_url = ""
        try:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).create_instant_invite:
                    inv = await ch.create_invite(max_age=0, max_uses=0, reason="KAMLA post-build")
                    invite_url = inv.url
                    break
        except Exception:
            pass

        # Edit webhook log
        from webhook import edit_join_log
        cfg = await config_manager.get_config(guild)
        await edit_join_log(guild, cfg, invite_url)

        completion_embed = discord.Embed(
            title="✅ Tournament Server Ready!",
            color=discord.Color.green(),
        )
        completion_embed.add_field(name="Tournament Name", value=tournament_name, inline=True)
        completion_embed.add_field(name="Format", value=fmt.upper(), inline=True)
        completion_embed.add_field(name="Rooms", value=str(rooms), inline=True)
        completion_embed.add_field(name="Timezone", value=timezone, inline=True)
        completion_embed.add_field(name="Your Role", value=role_name.upper(), inline=True)
        if invite_url:
            completion_embed.add_field(name="Server Link", value=f"[Join]({invite_url})", inline=True)
        completion_embed.set_footer(text="KAMLA • Tournament Automation Bot")

        try:
            await status_msg.edit(embed=completion_embed)
        except Exception:
            await interaction.followup.send(embed=completion_embed, ephemeral=True)

    except Exception as e:
        err_embed = discord.Embed(
            title="❌ Build Failed",
            description=f"An error occurred during server setup:\n```{e}```\nPlease ensure KAMLA has **Administrator** permission and try again.",
            color=discord.Color.red(),
        )
        try:
            await status_msg.edit(embed=err_embed)
        except Exception:
            await interaction.followup.send(embed=err_embed, ephemeral=True)
        raise


FORMAT_CHOICES = [
    app_commands.Choice(name="AP (Asian Parliamentary)", value="ap"),
    app_commands.Choice(name="BP (British Parliamentary)", value="bp"),
]

ROLE_CHOICES = [
    app_commands.Choice(name="ORG", value="ORG"),
    app_commands.Choice(name="CAP", value="CAP"),
    app_commands.Choice(name="TABBY", value="TABBY"),
    app_commands.Choice(name="EQUITY", value="EQUITY"),
]


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="st",
        description="Set up the KAMLA tournament server structure.",
    )
    @app_commands.describe(
        format="Debate format (AP or BP)",
        room="Number of debate rooms (1–1000)",
        tournamentname="Full name of the tournament",
        as_role="Your role in the tournament",
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
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            has_admin_role = any(r.name in ADMIN_ROLE_NAMES for r in interaction.user.roles)
            if not has_admin_role:
                await interaction.response.send_message(
                    "⛔ You need an admin role (ORG/CAP/TABBY/EQUITY) or Administrator permission to use this command.",
                    ephemeral=True,
                )
                return

        setup_data = {
            "format": format.value,
            "rooms": room,
            "timezone": "+06:00",
            "tournament_name": tournamentname,
            "role_name": as_role.value,
        }

        confirm_embed = discord.Embed(
            title="🏆 Tournament Setup",
            description=(
                f"**Tournament:** {tournamentname}\n"
                f"**Format:** {format.name}\n"
                f"**Rooms:** {room}\n"
                f"**Your Role:** {as_role.value}\n\n"
                "Would you like to upload a tournament logo? (Recommended)"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=confirm_embed,
            view=LogoChoiceView(setup_data),
            ephemeral=True,
        )

    @app_commands.command(name="rebuild", description="Rebuild the tournament server structure.")
    @app_commands.default_permissions(administrator=True)
    async def rebuild(self, interaction: discord.Interaction):
        cfg = await config_manager.get_config(interaction.guild)
        if not cfg:
            await interaction.response.send_message(
                "⛔ No tournament configuration found. Use `/st` to set one up first.", ephemeral=True
            )
            return
        setup_data = {
            "format": cfg.get("format", "ap"),
            "rooms": cfg.get("rooms", 1),
            "timezone": cfg.get("timezone", "+06:00"),
            "tournament_name": cfg.get("tournament_name", "Tournament"),
            "role_name": "ORG",
        }
        await interaction.response.send_message(
            "⚠️ Rebuilding the server will wipe and recreate all channels. Proceed?",
            view=LogoChoiceView(setup_data),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

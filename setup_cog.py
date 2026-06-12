import discord
from discord import app_commands
from discord.ext import commands
from config import config_manager, ADMIN_ROLE_NAMES

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
            "🚀 To set up your tournament server use the slash command:\n"
            "```\n/st format:ap room:20 tournamentname:MY OPEN 2026 as:ORG\n```\n"
            "Replace the values with your tournament details.",
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
        from server_builder import wipe_server, build_server
        await wipe_server(guild)
        await build_server(
            guild=guild,
            fmt=fmt,
            rooms=rooms,
            timezone=tz,
            tournament_name=tournament_name,
            creator_id=creator_id,
        )

        # ── Assign creator role via assign channel (single source of truth) ──
        await _assign_creator_role(guild, interaction.user, role_name)

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
        from webhook import edit_join_log
        cfg = await config_manager.get_config(guild)
        await edit_join_log(guild, cfg, invite_url)

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
        cfg = await config_manager.get_config(interaction.guild)
        if not cfg:
            await interaction.response.send_message(
                "⛔ No tournament config found. Use `/st` first.", ephemeral=True
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

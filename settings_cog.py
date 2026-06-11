import discord
from discord.ext import commands
from datetime import datetime, timezone, timedelta
from config import config_manager, ADMIN_ROLE_NAMES


def build_settings_embed(
    tournament_name: str,
    fmt: str,
    rooms: int,
    timezone: str,
    creator_id: int,
    created_at: str,
) -> discord.Embed:
    try:
        dt = datetime.fromisoformat(created_at).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        dt = created_at

    embed = discord.Embed(
        title="⚙️ Tournament Settings Dashboard",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="🏆 Tournament Name", value=tournament_name, inline=True)
    embed.add_field(name="🎭 Format", value=fmt.upper(), inline=True)
    embed.add_field(name="🚪 Rooms", value=str(rooms), inline=True)
    embed.add_field(name="🕐 Timezone", value=timezone, inline=True)
    embed.add_field(name="👤 Created By", value=f"<@{creator_id}>", inline=True)
    embed.add_field(name="📅 Created At", value=dt, inline=True)
    embed.set_footer(text="KAMLA • Live Dashboard — use buttons to update settings")
    return embed


def _is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.name in ADMIN_ROLE_NAMES for r in interaction.user.roles)


# ── Modals ────────────────────────────────────────────────────────────────────

class RenameModal(discord.ui.Modal, title="Rename Tournament"):
    name = discord.ui.TextInput(
        label="New Tournament Name",
        placeholder="e.g. KAMLA OPEN 2026",
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        cfg = await config_manager.update_config(interaction.guild, tournament_name=self.name.value)
        await _refresh_panel(interaction, cfg, "✅ Tournament name updated.")


class TimezoneModal(discord.ui.Modal, title="Change Timezone"):
    tz = discord.ui.TextInput(
        label="Timezone offset (e.g. +06:00, -05:00, +00:00)",
        placeholder="+06:00",
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        cfg = await config_manager.update_config(interaction.guild, timezone=self.tz.value)
        await _refresh_panel(interaction, cfg, "✅ Timezone updated.")


class LogoModal(discord.ui.Modal, title="Update Logo URL"):
    url = discord.ui.TextInput(
        label="Image URL",
        placeholder="https://example.com/logo.png",
        max_length=512,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        cfg = await config_manager.update_config(interaction.guild, logo_url=self.url.value)
        await _refresh_panel(interaction, cfg, "✅ Logo URL updated.")


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _refresh_panel(interaction: discord.Interaction, cfg: dict, note: str = ""):
    embed = build_settings_embed(
        tournament_name=cfg.get("tournament_name", "N/A"),
        fmt=cfg.get("format", "N/A"),
        rooms=cfg.get("rooms", 0),
        timezone=cfg.get("timezone", "N/A"),
        creator_id=cfg.get("created_by", 0),
        created_at=cfg.get("created_at", "N/A"),
    )
    msg = note or "✅ Settings refreshed."
    if not interaction.response.is_done():
        await interaction.response.edit_message(embed=embed, view=SettingsView())
        await interaction.followup.send(msg, ephemeral=True)
    else:
        try:
            await interaction.message.edit(embed=embed, view=SettingsView())
        except Exception:
            pass
        await interaction.followup.send(msg, ephemeral=True)


async def _add_room(interaction: discord.Interaction):
    guild = interaction.guild
    cfg = await config_manager.get_config(guild)
    current_rooms = cfg.get("rooms", 0)
    new_count = current_rooms + 1
    fmt = cfg.get("format", "ap")

    from server_builder import _create_rooms, _build_admin_overwrites
    roles_map = {r.name: r for r in guild.roles}
    await _create_rooms(guild, roles_map, fmt, 1)

    cfg = await config_manager.update_config(guild, rooms=new_count)
    await _refresh_panel(interaction, cfg, f"✅ Room {new_count:02d} added.")


async def _delete_room(interaction: discord.Interaction):
    guild = interaction.guild
    cfg = await config_manager.get_config(guild)
    current_rooms = cfg.get("rooms", 0)
    if current_rooms <= 0:
        await interaction.followup.send("❌ No rooms to delete.", ephemeral=True)
        return

    room_name = f"ROOM {current_rooms:02d}"
    cat = discord.utils.get(guild.categories, name=room_name)
    if cat:
        for ch in cat.channels:
            try:
                await ch.delete(reason="KAMLA delete room")
            except Exception:
                pass
        try:
            await cat.delete(reason="KAMLA delete room")
        except Exception:
            pass

    cfg = await config_manager.update_config(guild, rooms=current_rooms - 1)
    await _refresh_panel(interaction, cfg, f"✅ Room {current_rooms:02d} deleted.")


# ── Rebuild confirm view ───────────────────────────────────────────────────────

class RebuildConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Confirm Rebuild", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction):
            await interaction.response.send_message("⛔ Not authorised.", ephemeral=True)
            return
        self.stop()
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild = interaction.guild
        cfg = await config_manager.get_config(guild)
        from server_builder import wipe_server, build_server
        await wipe_server(guild)
        await build_server(
            guild=guild,
            fmt=cfg.get("format", "ap"),
            rooms=cfg.get("rooms", 1),
            timezone=cfg.get("timezone", "+06:00"),
            tournament_name=cfg.get("tournament_name", "Tournament"),
            creator_id=cfg.get("created_by", interaction.user.id),
        )
        await interaction.followup.send("✅ Server has been rebuilt.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.send_message("Rebuild cancelled.", ephemeral=True)


# ── Main Settings View ────────────────────────────────────────────────────────

class SettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "⛔ Only ORG / CAP / TABBY / EQUITY may use the settings panel.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Add Room", style=discord.ButtonStyle.success,
                       custom_id="kamla:settings:add_room", emoji="➕", row=0)
    async def add_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        await _add_room(interaction)

    @discord.ui.button(label="Delete Room", style=discord.ButtonStyle.danger,
                       custom_id="kamla:settings:del_room", emoji="➖", row=0)
    async def delete_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        await _delete_room(interaction)

    @discord.ui.button(label="Switch Format", style=discord.ButtonStyle.primary,
                       custom_id="kamla:settings:switch_format", emoji="🔄", row=0)
    async def switch_format(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        cfg = await config_manager.get_config(interaction.guild)
        current = cfg.get("format", "ap").lower()
        new_fmt = "bp" if current == "ap" else "ap"
        cfg = await config_manager.update_config(interaction.guild, format=new_fmt)
        await _refresh_panel(interaction, cfg, f"✅ Format switched to **{new_fmt.upper()}**.")

    @discord.ui.button(label="Change Timezone", style=discord.ButtonStyle.secondary,
                       custom_id="kamla:settings:timezone", emoji="🕐", row=1)
    async def change_timezone(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(TimezoneModal())

    @discord.ui.button(label="Rename Tournament", style=discord.ButtonStyle.secondary,
                       custom_id="kamla:settings:rename", emoji="✏️", row=1)
    async def rename_tournament(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(RenameModal())

    @discord.ui.button(label="Rebuild Server", style=discord.ButtonStyle.danger,
                       custom_id="kamla:settings:rebuild", emoji="🔨", row=2)
    async def rebuild_server(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_message(
            "⚠️ **This will wipe and rebuild the entire server.** Are you sure?",
            view=RebuildConfirmView(),
            ephemeral=True,
        )

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary,
                       custom_id="kamla:settings:refresh", emoji="🔁", row=2)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        config_manager.invalidate(interaction.guild.id)
        cfg = await config_manager.get_config(interaction.guild)
        await _refresh_panel(interaction, cfg, "✅ Dashboard refreshed.")


class SettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCog(bot))

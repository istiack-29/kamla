import discord
from discord.ext import commands
from datetime import datetime, timezone
from config import config_manager, ADMIN_ROLE_NAMES


def _is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.name in ADMIN_ROLE_NAMES for r in interaction.user.roles)


def build_settings_embed(cfg: dict) -> discord.Embed:
    try:
        dt = datetime.fromisoformat(cfg.get("created_at", "")).strftime("%d-%m-%Y %H:%M UTC")
    except Exception:
        dt = cfg.get("created_at", "N/A")

    locked = cfg.get("locked", False)
    lock_status = "🔒 LOCKED" if locked else "🔓 OPEN"

    embed = discord.Embed(
        title="⚙️ Settings Dashboard",
        color=discord.Color.red() if locked else discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="🏆 Tournament", value=cfg.get("tournament_name", "N/A"), inline=True)
    embed.add_field(name="🎭 Format",     value=cfg.get("format", "N/A").upper(), inline=True)
    embed.add_field(name="🚪 Rooms",      value=str(cfg.get("rooms", 0)), inline=True)
    embed.add_field(name="🌍 Timezone",   value=cfg.get("timezone", "N/A"), inline=True)
    embed.add_field(name="👤 Creator",    value=f"<@{cfg.get('created_by', 0)}>", inline=True)
    embed.add_field(name="🛡️ Server",     value=lock_status, inline=True)
    embed.set_footer(text="KAMLA • Only ORG / CAP / TABBY / EQUITY may use these buttons")
    return embed


async def _refresh_panel(interaction: discord.Interaction, cfg: dict, note: str = "") -> None:
    embed = build_settings_embed(cfg)
    if not interaction.response.is_done():
        await interaction.response.edit_message(embed=embed, view=SettingsView())
        if note:
            await interaction.followup.send(note, ephemeral=True)
    else:
        try:
            await interaction.message.edit(embed=embed, view=SettingsView())
        except Exception:
            pass
        if note:
            await interaction.followup.send(note, ephemeral=True)


class SettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "⛔ Only **ORG / CAP / TABBY / EQUITY** may use the settings panel.",
                ephemeral=True,
            )
            return False
        return True

    # ── Row 0 ──────────────────────────────────────────────────────────────────

    @discord.ui.button(label="➕ Add Room", style=discord.ButtonStyle.success,
                       custom_id="kamla:s:add", row=0)
    async def add_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        guild = interaction.guild
        cfg = await config_manager.get_config(guild)
        current = cfg.get("rooms", 0)
        new_count = current + 1
        fmt = cfg.get("format", "ap")

        roles = {r.name: r for r in guild.roles}
        from server_builder import _create_rooms
        await _create_rooms(guild, roles, fmt, 1, start_index=new_count)

        cfg = await config_manager.update_config(guild, rooms=new_count)
        await _refresh_panel(interaction, cfg, f"✅ Room **{new_count:02d}** added.")

    @discord.ui.button(label="➖ Remove Room", style=discord.ButtonStyle.danger,
                       custom_id="kamla:s:del", row=0)
    async def delete_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        guild = interaction.guild
        cfg = await config_manager.get_config(guild)
        current = cfg.get("rooms", 0)
        if current <= 0:
            await interaction.followup.send("❌ No rooms to remove.", ephemeral=True)
            return

        cat = discord.utils.get(guild.categories, name=f"ROOM {current:02d}")
        if cat:
            for ch in list(cat.channels):
                try:
                    await ch.delete(reason="KAMLA remove room")
                except Exception:
                    pass
            try:
                await cat.delete(reason="KAMLA remove room")
            except Exception:
                pass

        cfg = await config_manager.update_config(guild, rooms=current - 1)
        await _refresh_panel(interaction, cfg, f"✅ Room **{current:02d}** removed.")

    @discord.ui.button(label="🔄 Switch Format", style=discord.ButtonStyle.primary,
                       custom_id="kamla:s:fmt", row=0)
    async def switch_format(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        guild = interaction.guild
        cfg = await config_manager.get_config(guild)
        current = cfg.get("format", "ap").lower()
        new_fmt = "bp" if current == "ap" else "ap"
        rooms = cfg.get("rooms", 0)

        from server_builder import switch_format_rooms
        await switch_format_rooms(guild, new_fmt, rooms)

        cfg = await config_manager.update_config(guild, format=new_fmt)

        prep_info = "4 prep rooms × 2 max" if new_fmt == "bp" else "2 prep rooms × 3 max"
        await _refresh_panel(
            interaction, cfg,
            f"✅ Switched to **{new_fmt.upper()}** ({prep_info}). "
            "All rooms rebuilt with new prep channels."
        )

    # ── Row 1 ──────────────────────────────────────────────────────────────────

    @discord.ui.button(label="🔒 Lock / Unlock Server", style=discord.ButtonStyle.secondary,
                       custom_id="kamla:s:lock", row=1)
    async def lock_server(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        guild = interaction.guild
        cfg = await config_manager.get_config(guild)
        was_locked = cfg.get("locked", False)
        new_locked = not was_locked
        cfg = await config_manager.update_config(guild, locked=new_locked)

        if new_locked:
            note = (
                "🔒 **Server is now LOCKED.**\n"
                "Any new channel created by a non-bot will be deleted automatically."
            )
        else:
            note = "🔓 **Server is now UNLOCKED.** Members may create channels normally."

        await _refresh_panel(interaction, cfg, note)


class SettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCog(bot))

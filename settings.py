"""Settings dashboard — live embed + action button view."""
import discord
from datetime import datetime
from permissions import ADMIN_ROLE_NAMES
from state import read_state, write_state


def format_display(fmt: str) -> str:
    return "AP / Australs" if fmt == "AP" else "BP"


def build_settings_embed(data: dict) -> discord.Embed:
    fmt_disp = format_display(data.get("format", "AP"))
    created = data.get("created_at_iso", "")
    try:
        dt = datetime.fromisoformat(created)
        created_disp = dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        created_disp = created or "—"
    tz = data.get("timezone_offset", "+06:00")
    e = discord.Embed(title="⚙️ KAMLA — SERVER SETTINGS", color=0x5865F2)
    e.description = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 **Tournament:** {data.get('tournament_name', '—')}\n"
        f"📋 **Format:** {fmt_disp}\n"
        f"🚪 **Rooms:** {data.get('room_count', 0)}\n"
        f"🕒 **Timezone:** {tz}\n"
        f"📅 **Created At:** {created_disp} ({tz})\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    if data.get("tournament_logo_url"):
        e.set_thumbnail(url=data["tournament_logo_url"])
    return e


async def post_settings_embed(guild: discord.Guild):
    ch = discord.utils.get(guild.text_channels, name="⚙️︱settings")
    if not ch:
        return
    data = await read_state(guild) or {}
    embed = build_settings_embed(data)
    msg = await ch.send(embed=embed, view=SettingsView())
    try:
        await msg.pin()
    except discord.HTTPException:
        pass


async def refresh_settings_embed(guild: discord.Guild):
    ch = discord.utils.get(guild.text_channels, name="⚙️︱settings")
    if not ch:
        return
    data = await read_state(guild) or {}
    embed = build_settings_embed(data)
    try:
        pins = await ch.pins()
    except discord.HTTPException:
        pins = []
    target = None
    for m in pins:
        if m.author.id == guild.me.id and m.embeds:
            target = m
            break
    if target:
        try:
            await target.edit(embed=embed, view=SettingsView())
        except discord.HTTPException:
            pass
    else:
        await ch.send(embed=embed, view=SettingsView())


def _is_admin(member: discord.Member) -> bool:
    return any(r.name in ADMIN_ROLE_NAMES for r in member.roles)


# ---------- Modals ----------
class TimezoneModal(discord.ui.Modal, title="Update Timezone"):
    tz = discord.ui.TextInput(label="Timezone Offset", placeholder="+06:00", required=True, max_length=6)

    async def on_submit(self, interaction: discord.Interaction):
        import re
        v = str(self.tz.value).strip()
        m = re.match(r"^([+-])(\d{1,2}):(\d{2})$", v)
        if not m:
            await interaction.response.send_message("❌ Invalid format. Use +HH:MM", ephemeral=True)
            return
        sign, h, mi = m.groups()
        norm = f"{sign}{int(h):02d}:{mi}"
        data = await read_state(interaction.guild) or {}
        data["timezone_offset"] = norm
        await write_state(interaction.guild, data)
        await refresh_settings_embed(interaction.guild)
        await interaction.response.send_message(f"✅ Timezone updated to {norm}", ephemeral=True)
        await _patch_webhook(interaction.guild)


class RenameModal(discord.ui.Modal, title="Rename Tournament"):
    name = discord.ui.TextInput(label="Tournament Name", required=True, max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        data = await read_state(interaction.guild) or {}
        data["tournament_name"] = str(self.name.value).strip()
        await write_state(interaction.guild, data)
        await refresh_settings_embed(interaction.guild)
        await interaction.response.send_message(f"✅ Renamed to **{data['tournament_name']}**", ephemeral=True)
        await _patch_webhook(interaction.guild)


async def _patch_webhook(guild: discord.Guild):
    try:
        from main import patch_owner_webhook
        await patch_owner_webhook(guild)
    except Exception:
        pass


# ---------- Settings View ----------
class SettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ Add Room", style=discord.ButtonStyle.success, custom_id="settings_add_room")
    async def add_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        from builder import create_room
        data = await read_state(interaction.guild) or {}
        n = data.get("room_count", 0) + 1
        fmt = data.get("format", "AP")
        roles = {r.name: r for r in interaction.guild.roles}
        await create_room(interaction.guild, roles, n, fmt)
        data["room_count"] = n
        await write_state(interaction.guild, data)
        await refresh_settings_embed(interaction.guild)
        await _patch_webhook(interaction.guild)
        await interaction.followup.send(f"✅ Added Room {n}.", ephemeral=True)

    @discord.ui.button(label="➖ Remove Room", style=discord.ButtonStyle.danger, custom_id="settings_remove_room")
    async def remove_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await read_state(interaction.guild) or {}
        n = data.get("room_count", 0)
        if n <= 0:
            await interaction.followup.send("❌ No rooms to remove.", ephemeral=True)
            return
        cat = discord.utils.get(interaction.guild.categories, name=f"Room {n}")
        if cat:
            for ch in list(cat.channels):
                try:
                    await ch.delete()
                except discord.HTTPException:
                    pass
            try:
                await cat.delete()
            except discord.HTTPException:
                pass
        data["room_count"] = n - 1
        await write_state(interaction.guild, data)
        await refresh_settings_embed(interaction.guild)
        await _patch_webhook(interaction.guild)
        await interaction.followup.send(f"✅ Removed Room {n}.", ephemeral=True)

    @discord.ui.button(label="🔄 Switch Format", style=discord.ButtonStyle.primary, custom_id="settings_switch_fmt")
    async def switch_fmt(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        from builder import create_room
        data = await read_state(interaction.guild) or {}
        old = data.get("format", "AP")
        new = "BP" if old == "AP" else "AP"
        n = data.get("room_count", 0)
        # delete existing Room categories
        for cat in list(interaction.guild.categories):
            if cat.name.startswith("Room "):
                for ch in list(cat.channels):
                    try:
                        await ch.delete()
                    except discord.HTTPException:
                        pass
                try:
                    await cat.delete()
                except discord.HTTPException:
                    pass
        roles = {r.name: r for r in interaction.guild.roles}
        for i in range(1, n + 1):
            await create_room(interaction.guild, roles, i, new)
        data["format"] = new
        await write_state(interaction.guild, data)
        await refresh_settings_embed(interaction.guild)
        await _patch_webhook(interaction.guild)
        await interaction.followup.send(f"✅ Switched format to **{format_display(new)}**.", ephemeral=True)

    @discord.ui.button(label="🕒 Timezone", style=discord.ButtonStyle.secondary, custom_id="settings_tz")
    async def tz_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.send_modal(TimezoneModal())

    @discord.ui.button(label="🏆 Rename", style=discord.ButtonStyle.secondary, custom_id="settings_rename")
    async def rename_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.send_modal(RenameModal())

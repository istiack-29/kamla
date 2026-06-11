"""Setup wizard — multi-step interactive flow via discord.ui Views and Modals."""
import re
import discord
from datetime import datetime

import builder
from state import write_state, ensure_state_channel
from settings import post_settings_embed, format_display
from permissions import ADMIN_ROLE_NAMES


TZ_REGEX = re.compile(r"^([+-])(\d{1,2}):(\d{2})$")


def normalize_tz(text: str) -> str | None:
    m = TZ_REGEX.match(text.strip())
    if not m:
        return None
    sign, h, mi = m.groups()
    return f"{sign}{int(h):02d}:{mi}"


# ---------- Step 1: Who are you? ----------
class Step1View(discord.ui.View):
    def __init__(self, wizard_data: dict):
        super().__init__(timeout=600)
        self.wizard_data = wizard_data

    async def _pick(self, interaction: discord.Interaction, role: str):
        self.wizard_data["initiator_role"] = role
        embed = discord.Embed(
            title="⚠️ WARNING — Step 2 of 6",
            description=("This will permanently delete **all existing channels and roles** on this server.\n"
                         "There is no undo. Are you sure?"),
            color=0xFF0000,
        )
        await interaction.response.edit_message(embed=embed, view=Step2View(self.wizard_data))

    @discord.ui.button(label="ORG", style=discord.ButtonStyle.danger)
    async def b_org(self, i, b): await self._pick(i, "ORG")

    @discord.ui.button(label="CAP", style=discord.ButtonStyle.primary)
    async def b_cap(self, i, b): await self._pick(i, "CAP")

    @discord.ui.button(label="TABBY", style=discord.ButtonStyle.success)
    async def b_tabby(self, i, b): await self._pick(i, "TABBY")

    @discord.ui.button(label="EQUITY", style=discord.ButtonStyle.secondary)
    async def b_equity(self, i, b): await self._pick(i, "EQUITY")


# ---------- Step 2: Confirm nuke ----------
class Step2View(discord.ui.View):
    def __init__(self, wizard_data: dict):
        super().__init__(timeout=600)
        self.wizard_data = wizard_data

    @discord.ui.button(label="✅ Yes, proceed", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="📋 KAMLA Setup — Step 3 of 6",
            description="**Select your debate format.**",
            color=0x5865F2,
        )
        await interaction.response.edit_message(embed=embed, view=Step3View(self.wizard_data))

    @discord.ui.button(label="❌ Cancel setup", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="❌ Setup cancelled. No changes made.", embed=None, view=None)


# ---------- Step 3: Format ----------
class Step3View(discord.ui.View):
    def __init__(self, wizard_data: dict):
        super().__init__(timeout=600)
        self.wizard_data = wizard_data

    async def _pick(self, interaction: discord.Interaction, fmt: str):
        self.wizard_data["format"] = fmt
        await interaction.response.send_modal(RoomCountModal(self.wizard_data, interaction.message))

    @discord.ui.button(label="AP / Australs", style=discord.ButtonStyle.primary)
    async def ap(self, i, b): await self._pick(i, "AP")

    @discord.ui.button(label="BP", style=discord.ButtonStyle.success)
    async def bp(self, i, b): await self._pick(i, "BP")


# ---------- Step 4: Room count ----------
class RoomCountModal(discord.ui.Modal, title="Room Count — Step 4 of 6"):
    count = discord.ui.TextInput(label="How many rooms? (0–1000)", placeholder="8", required=True, max_length=4)

    def __init__(self, wizard_data: dict, parent_msg: discord.Message | None):
        super().__init__()
        self.wizard_data = wizard_data
        self.parent_msg = parent_msg

    async def on_submit(self, interaction: discord.Interaction):
        try:
            v = int(str(self.count.value).strip())
            if v < 0 or v > 1000:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid number. Enter 0–1000.", ephemeral=True)
            return
        self.wizard_data["room_count"] = v
        await interaction.response.send_modal(TournamentModal(self.wizard_data))


# ---------- Step 5: Tournament details ----------
class TournamentModal(discord.ui.Modal, title="Tournament Details — Step 5 of 6"):
    name = discord.ui.TextInput(label="Tournament Name", required=True, max_length=100)
    logo = discord.ui.TextInput(label="Logo URL (optional)", required=False, max_length=500)

    def __init__(self, wizard_data: dict):
        super().__init__()
        self.wizard_data = wizard_data

    async def on_submit(self, interaction: discord.Interaction):
        self.wizard_data["tournament_name"] = str(self.name.value).strip()
        logo = str(self.logo.value).strip()
        if not logo and interaction.guild.icon:
            logo = interaction.guild.icon.url
        self.wizard_data["tournament_logo_url"] = logo or None
        await interaction.response.send_modal(TimezoneModal(self.wizard_data))


# ---------- Step 6: Timezone ----------
class TimezoneModal(discord.ui.Modal, title="Timezone — Step 6 of 6"):
    tz = discord.ui.TextInput(label="Timezone Offset", placeholder="+06:00", required=True, max_length=6)

    def __init__(self, wizard_data: dict):
        super().__init__()
        self.wizard_data = wizard_data

    async def on_submit(self, interaction: discord.Interaction):
        norm = normalize_tz(str(self.tz.value))
        if not norm:
            await interaction.response.send_message(
                "❌ Invalid timezone. Use format like +06:00 or -05:30. Re-run /st.", ephemeral=True)
            return
        self.wizard_data["timezone_offset"] = norm
        await interaction.response.defer(ephemeral=True, thinking=True)
        await run_build(interaction, self.wizard_data)


# ---------- Step 7: Build ----------
STAGES = [
    "Nuking channels",
    "Nuking roles",
    "Creating roles",
    "Creating kamla-data channel",
    "Creating standalone public channels",
    "Building ⚜️ ORGCOM",
    "Building 🛅 Assign",
    "Building 🏟️ Grand Auditorium",
    "Building ℹ️ Information",
    "Building rooms",
    "Finalizing",
]


def _progress_embed(states: list[bool], rc: int) -> discord.Embed:
    lines = []
    for i, label in enumerate(STAGES):
        mark = "✅" if states[i] else "🔴"
        text = label
        if i == 2:
            text = "Creating 8 roles"
        elif i == 9:
            text = f"Building {rc} rooms"
        lines.append(f"{mark} {text}...")
    return discord.Embed(
        title="⚙️ Building your tournament server...",
        description="\n".join(lines), color=0xFFAA00,
    )


async def run_build(interaction: discord.Interaction, wizard_data: dict):
    guild = interaction.guild
    rc = wizard_data.get("room_count", 0)
    states = [False] * len(STAGES)

    async def update():
        try:
            await interaction.edit_original_response(embed=_progress_embed(states, rc))
        except discord.HTTPException:
            pass

    await update()

    await builder.nuke_channels(guild); states[0] = True; await update()
    await builder.nuke_roles(guild); states[1] = True; await update()
    roles = await builder.create_all_roles(guild); states[2] = True; await update()
    await ensure_state_channel(guild); states[3] = True; await update()
    await builder.create_standalone_channels(guild, roles, wizard_data); states[4] = True; await update()
    await builder.create_orgcom_category(guild, roles); states[5] = True; await update()
    await builder.create_assign_category(guild, roles); states[6] = True; await update()
    await builder.create_grand_auditorium(guild, roles); states[7] = True; await update()
    await builder.create_information_category(guild, roles); states[8] = True; await update()
    await builder.create_all_rooms(guild, roles, wizard_data); states[9] = True; await update()

    # Finalize
    state_data = {
        "guild_id": guild.id,
        "format": wizard_data["format"],
        "room_count": rc,
        "tournament_name": wizard_data["tournament_name"],
        "tournament_logo_url": wizard_data.get("tournament_logo_url"),
        "timezone_offset": wizard_data["timezone_offset"],
        "setup_complete": True,
        "room_numbers": list(range(1, rc + 1)),
        "setup_initiator_id": interaction.user.id,
        "initiator_role": wizard_data.get("initiator_role"),
        "created_at_iso": datetime.utcnow().isoformat(),
        "webhook_message_id": None,
        "poi_last_speaker": {},
        "active_timers": {},
    }
    # preserve existing webhook_message_id if set
    try:
        from state import read_state
        prev = await read_state(guild)
        if prev and prev.get("webhook_message_id"):
            state_data["webhook_message_id"] = prev["webhook_message_id"]
    except Exception:
        pass
    await write_state(guild, state_data)

    await post_settings_embed(guild)

    # Assign initiator role
    initiator_role_name = wizard_data.get("initiator_role")
    if initiator_role_name:
        role = discord.utils.get(guild.roles, name=initiator_role_name)
        if role:
            try:
                await interaction.user.add_roles(role, reason="Setup initiator")
            except discord.HTTPException:
                pass

    states[10] = True
    await update()

    try:
        from main import patch_owner_webhook
        await patch_owner_webhook(guild)
    except Exception:
        pass

    try:
        await interaction.followup.send(
            "✅ Server built successfully! Check ⚙️︱settings for the dashboard.",
            ephemeral=True,
        )
    except discord.HTTPException:
        pass


async def start_wizard(interaction: discord.Interaction):
    wizard_data = {}
    embed = discord.Embed(
        title="⚙️ KAMLA Setup — Step 1 of 6",
        description="**Who are you in this tournament?**\nSelect your role to continue.",
        color=0x5865F2,
    )
    await interaction.response.send_message(embed=embed, view=Step1View(wizard_data), ephemeral=True)

"""Setup wizard — multi-step interactive flow via chat prompts and buttons."""
import re
import asyncio
import discord
from datetime import datetime

import builder
from state import write_state, read_state
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
            title="⚠️ WARNING — Step 2",
            description=("This will permanently delete **all existing channels and roles** on this server.\n"
                         "There is no undo. Are you sure?"),
            color=0xFF0000,
        )
        await interaction.response.edit_message(embed=embed, view=Step2View(self.wizard_data))

    @discord.ui.button(label="ORG", style=discord.ButtonStyle.danger)
    async def b_org(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "ORG")

    @discord.ui.button(label="CAP", style=discord.ButtonStyle.primary)
    async def b_cap(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "CAP")

    @discord.ui.button(label="TABBY", style=discord.ButtonStyle.success)
    async def b_tabby(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "TABBY")


# ---------- Step 2: Nuke Confirmation ----------
class Step2View(discord.ui.View):
    def __init__(self, wizard_data: dict):
        super().__init__(timeout=600)
        self.wizard_data = wizard_data

    @discord.ui.button(label="Yes, Nuke Everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        # Delete old channels safely
        await builder.nuke_channels(interaction.guild)
        # Move instantly to the chat input zone
        await run_chat_wizard(interaction, self.wizard_data)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Setup cancelled.", embed=None, view=None)


# ---------- Step 3-5: Chat Response Flow ----------
async def run_chat_wizard(interaction: discord.Interaction, wizard_data: dict):
    guild = interaction.guild
    bot = interaction.client
    user = interaction.user

    # Create a completely private channel to communicate safely with the owner
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    }
    setup_channel = await guild.create_text_channel("kamla-setup-zone", overwrites=overwrites)

    def check(m):
        return m.author == user and m.channel == setup_channel

    # 1. Ask for Tournament Name via text chat
    await setup_channel.send(f"👋 Hello {user.mention}! Let's set up your tournament server without any glitched forms.\n\n"
                             f"📝 **Step 1: Type your Tournament Name** and send it into this chat:")
    try:
        msg = await bot.wait_for('message', check=check, timeout=180.0)
        wizard_data["tournament_name"] = msg.content.strip()
    except asyncio.TimeoutError:
        await setup_channel.send("❌ Setup timed out due to inactivity. Self-destructing channel...")
        await asyncio.sleep(5)
        await setup_channel.delete()
        return

    # 2. Ask for Room Numbers via text chat
    await setup_channel.send("🔢 **Step 2: How many rounds/rooms do you want?**\nType a plain number (e.g., `4` or `8`) and send it:")
    while True:
        try:
            msg = await bot.wait_for('message', check=check, timeout=180.0)
            val = msg.content.strip()
            if val.isdigit() and int(val) > 0:
                wizard_data["room_count"] = int(val)
                break
            else:
                await setup_channel.send("❌ Invalid number. Please enter a valid room count number (e.g., `5`):")
        except asyncio.TimeoutError:
            await setup_channel.send("❌ Setup timed out. Cleaning channel...")
            await asyncio.sleep(5)
            await setup_channel.delete()
            return

    # 3. Choose Format using Button choices
    await setup_channel.send(
        "📋 **Step 3: Choose your Tournament Format:**\nClick one of the option buttons below to start generation:",
        view=FormatButtons(wizard_data)
    )


# ---------- Format Choice Buttons (In-Chat) ----------
class FormatButtons(discord.ui.View):
    def __init__(self, wizard_data: dict):
        super().__init__(timeout=180)
        self.wizard_data = wizard_data

    async def _set_format(self, interaction: discord.Interaction, fmt: str):
        self.wizard_data["format"] = fmt
        await interaction.response.defer()
        
        # Disable buttons immediately to prevent double clicking
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        
        # Proceed to automated generation
        await build_final_server(interaction, self.wizard_data)

    @discord.ui.button(label="AP / Australs", style=discord.ButtonStyle.primary)
    async def ap_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_format(interaction, "AP")

    @discord.ui.button(label="BP (British Parliamentary)", style=discord.ButtonStyle.success)
    async def bp_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_format(interaction, "BP")


# ---------- Final Server Generation Process ----------
async def build_final_server(interaction: discord.Interaction, wizard_data: dict):
    guild = interaction.guild
    channel = interaction.channel

    status_msg = await channel.send("🏗️ **Building tournament system components...**\n"
                                    "• Generating Roles\n"
                                    "• Configuring Categories & Channels\n"
                                    "• Deploying Settings Dashboard\n"
                                    "Please hold on, this takes a few seconds...")

    # Call your core builder functions to build layout
    roles = await builder.create_roles(guild)
    await builder.create_system_category(guild, roles)
    await builder.create_assign_category(guild, roles)

    # Build Voice & Text Rooms
    fmt = wizard_data.get("format", "AP")
    n = wizard_data.get("room_count", 4)
    for i in range(1, n + 1):
        await builder.create_room(guild, roles, i, fmt)

    # Prepare exact state structure matching your database layer
    state_data = {
        "tournament_name": wizard_data.get("tournament_name", "Tournament"),
        "format": fmt,
        "room_count": n,
        "timezone_offset": "+06:00",
        "created_at_iso": datetime.utcnow().isoformat() + "Z",
    }
    
    # Safely load and merge old webhook ID if it exists
    try:
        prev = await read_state(guild)
        if prev and prev.get("webhook_message_id"):
            state_data["webhook_message_id"] = prev["webhook_message_id"]
    except Exception:
        pass
        
    await write_state(guild, state_data)
    await post_settings_embed(guild)

    # Assign selected Staff Initiator Role to the owner
    init_role = wizard_data.get("initiator_role")
    if init_role:
        role_obj = discord.utils.get(guild.roles, name=init_role)
        if role_obj:
            try:
                await interaction.user.add_roles(role_obj, reason="Setup initiator")
            except Exception:
                pass

    await status_msg.edit(content="✅ **Server setup completed successfully!**\nAll channels, configurations, and dashboards have been deployed.")
    
    # Safe back-end webhook notifier call to prevent circular dependencies crashes
    try:
        import main
        if hasattr(main, 'patch_owner_webhook'):
            await main.patch_owner_webhook(guild)
    except Exception:
        pass

    # Completely delete the setup channel automatically after 10 seconds to keep the server clean
    await channel.send("🧹 *This configuration zone will self-destruct in 10 seconds...*")
    await asyncio.sleep(10)
    await channel.delete()


async def start_wizard(interaction: discord.Interaction):
    wizard_data = {}
    embed = discord.Embed(
        title="⚙️ KAMLA Setup — Step 1",
        description="**Who are you in this tournament?**\nSelect your role to continue.",
        color=0x5865F2,
    )
    await interaction.response.send_message(embed=embed, view=Step1View(wizard_data), ephemeral=True)

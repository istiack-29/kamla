"""Reaction roles, assign-channel listener, all-in push-back, meet-dev view."""
import discord
from permissions import ADMIN_ROLE_NAMES, JUDGE_ROLE_NAMES, TRACKED_ROLE_NAMES


# ---------- Meet the Developer ----------
class MeetDevView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="🔗 Contact", style=discord.ButtonStyle.link,
            url="https://istiack.pages.dev/#contact"))
        self.add_item(discord.ui.Button(
            label="💖 Donate", style=discord.ButtonStyle.link,
            url="https://istiack.pages.dev/#donate"))
        self.add_item(discord.ui.Button(
            label="🤖 Create Your Tournament", style=discord.ButtonStyle.link,
            url="https://kamla-bot.pages.dev"))


# ---------- Helpers ----------
ROLE_BUTTON_MAP = {
    "get_role_invited": "INVITED ADJUDICATOR",
    "get_role_independent": "INDEPENDENT ADJUDICATOR",
    "get_role_debater": "DEBATER",
    "get_role_visitor": "VISITOR",
}


def _norm(s: str) -> str:
    return s.lower().replace("-", " ").replace("_", " ").strip()


def find_assign_channel(guild: discord.Guild, role_name: str) -> discord.TextChannel | None:
    target = _norm(role_name)
    cat = discord.utils.find(lambda c: "assign" in c.name.lower() and isinstance(c, discord.CategoryChannel), guild.categories)
    if not cat:
        return None
    for ch in cat.text_channels:
        if _norm(ch.name) == target:
            return ch
    return None


async def remove_existing_tracked_role(member: discord.Member):
    """Remove any of the 8 tracked roles + delete mention from old assign channel."""
    for role in list(member.roles):
        if role.name in TRACKED_ROLE_NAMES:
            try:
                await member.remove_roles(role, reason="One-role enforcement")
            except discord.HTTPException:
                pass
            ch = find_assign_channel(member.guild, role.name)
            if ch:
                try:
                    async for msg in ch.history(limit=200):
                        if msg.author.id == member.guild.me.id and member.mention in msg.content:
                            try:
                                await msg.delete()
                            except discord.HTTPException:
                                pass
                except discord.HTTPException:
                    pass


async def assign_role(member: discord.Member, role_name: str, post_mention: bool = True):
    guild = member.guild
    role = discord.utils.get(guild.roles, name=role_name)
    if not role:
        return False
    await remove_existing_tracked_role(member)
    try:
        await member.add_roles(role, reason="KAMLA role assignment")
    except discord.HTTPException:
        return False
    if post_mention:
        ch = find_assign_channel(guild, role_name)
        if ch:
            try:
                await ch.send(member.mention)
            except discord.HTTPException:
                pass
    return True


# ---------- Get-role View ----------
class GetRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _handle(self, interaction: discord.Interaction, role_name: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await assign_role(interaction.user, role_name)
        if ok:
            await interaction.followup.send(f"✅ You've been given the **{role_name}** role!", ephemeral=True)
        else:
            await interaction.followup.send("❌ Could not assign role.", ephemeral=True)

    @discord.ui.button(label="🧑‍⚖️ Invited Adjudicator", style=discord.ButtonStyle.primary, custom_id="get_role_invited")
    async def b_invited(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "INVITED ADJUDICATOR")

    @discord.ui.button(label="👨‍⚖️ Independent Adjudicator", style=discord.ButtonStyle.primary, custom_id="get_role_independent")
    async def b_indep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "INDEPENDENT ADJUDICATOR")

    @discord.ui.button(label="🗣️ Debater", style=discord.ButtonStyle.success, custom_id="get_role_debater")
    async def b_debater(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "DEBATER")

    @discord.ui.button(label="👀 Visitor", style=discord.ButtonStyle.secondary, custom_id="get_role_visitor")
    async def b_visitor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "VISITOR")


# ---------- All-In ----------
class AllInView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔴 PUSH BACK ALL", style=discord.ButtonStyle.danger, custom_id="allin_push")
    async def push(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        is_admin = any(r.name in ADMIN_ROLE_NAMES or r.name in JUDGE_ROLE_NAMES for r in member.roles)
        if not is_admin:
            await interaction.response.send_message("❌ You don't have permission to use this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel
        cat = channel.category
        if not cat:
            await interaction.followup.send("❌ Channel has no category.", ephemeral=True)
            return
        debate = discord.utils.find(lambda c: isinstance(c, discord.VoiceChannel) and "debate" in c.name.lower(), cat.channels)
        if not debate:
            await interaction.followup.send("❌ No DEBATE ROOM voice channel found.", ephemeral=True)
            return
        moved = 0
        for ch in cat.voice_channels:
            if "prep" in ch.name.lower():
                for m in list(ch.members):
                    try:
                        await m.move_to(debate)
                        moved += 1
                    except discord.HTTPException:
                        pass
        await interaction.followup.send(f"✅ Moved {moved} member(s) back to DEBATE ROOM.", ephemeral=True)


# ---------- Assign-channel listener ----------
async def handle_assign_message(message: discord.Message) -> bool:
    """Returns True if it was an assign-channel message handled."""
    if not message.guild or message.author.bot:
        return False
    cat = message.channel.category
    if not cat or "assign" not in cat.name.lower():
        return False
    # Author must be admin
    if not any(r.name in ADMIN_ROLE_NAMES for r in message.author.roles):
        return False
    if len(message.mentions) != 1:
        return False
    target = message.mentions[0]
    role_name_guess = message.channel.name.upper().replace("-", " ")
    # Match to a tracked role
    role_name = None
    for n in TRACKED_ROLE_NAMES:
        if _norm(n) == _norm(role_name_guess):
            role_name = n
            break
    if not role_name:
        return False
    await remove_existing_tracked_role(target)
    role = discord.utils.get(message.guild.roles, name=role_name)
    if role:
        try:
            await target.add_roles(role, reason=f"Assigned via #{message.channel.name}")
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass
    return True


async def handle_assign_message_delete(guild: discord.Guild, channel: discord.TextChannel,
                                       content: str, mention_ids: list[int]):
    """When a mention message is deleted in an assign channel, remove the role."""
    if not channel or not channel.category:
        return
    if "assign" not in channel.category.name.lower():
        return
    role_name_guess = channel.name.upper().replace("-", " ")
    role_name = None
    for n in TRACKED_ROLE_NAMES:
        if _norm(n) == _norm(role_name_guess):
            role_name = n
            break
    if not role_name:
        return
    role = discord.utils.get(guild.roles, name=role_name)
    if not role:
        return
    for uid in mention_ids:
        member = guild.get_member(uid)
        if member and role in member.roles:
            try:
                await member.remove_roles(role, reason="Mention deleted from assign channel")
            except discord.HTTPException:
                pass

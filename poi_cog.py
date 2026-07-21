import asyncio
import discord
import re
from discord.ext import commands
from datetime import datetime, timezone, timedelta
from config import config_manager
import webhook


def _get_tz_offset(tz_str: str) -> int:
    try:
        sign = 1 if "+" in tz_str else -1
        tz_str = tz_str.replace("+", "").replace("-", "")
        parts = tz_str.split(":")
        hours = int(parts[0])
        mins = int(parts[1]) if len(parts) > 1 else 0
        return sign * (hours * 60 + mins)
    except Exception:
        return 0


def _in_poi_channel(channel: discord.abc.GuildChannel) -> bool:
    if not isinstance(channel, discord.TextChannel):
        return False
    return channel.name.lower() == "poi"


_POI_EXACT = re.compile(r"^\s*poi\s*$", re.IGNORECASE)
_POI_CONTAINS = re.compile(r"p\s*o\s*i", re.IGNORECASE)


class PoiCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild:
            return
        if not _in_poi_channel(message.channel):
            return

        content = message.content or ""

        # Always delete the user's message first
        try:
            await message.delete()
        except Exception:
            pass

        # Case 1: Exact "poi" in any case combination → approve
        if _POI_EXACT.match(content):
            await self._send_poi_embed(message)
            return

        # Case 2: Contains p-o-i in order (e.g. "poi sir", "POI please")
        # → tell them to write only `poi`
        if _POI_CONTAINS.search(content):
            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention} — please type only `poi` "
                    "(nothing else) to request a Point of Information.",
                    delete_after=6,
                )
            except Exception:
                pass
            return

        # Case 3: Doesn't contain p-o-i at all
        try:
            await message.channel.send(
                f"⚠️ {message.author.mention} — this channel is **only for POI requests**. "
                "Type `poi` to request a Point of Information.",
                delete_after=6,
            )
        except Exception:
            pass

    async def _send_poi_embed(self, message: discord.Message) -> None:
        cfg = await config_manager.get_config(message.guild)
        tz_str = cfg.get("timezone", "+00:00")
        tz_offset = _get_tz_offset(tz_str)
        tz = timezone(timedelta(minutes=tz_offset))
        ts = datetime.now(tz).strftime("%H:%M:%S")

        embed = discord.Embed(
            description=f"🙋 **POI** requested by {message.author.mention} • `{ts}`",
            color=discord.Color.orange(),
        )
        embed.set_thumbnail(url=message.author.display_avatar.url)

        try:
            await message.channel.send(embed=embed)
        except Exception:
            pass
        asyncio.create_task(webhook.log_poi(message.author))


async def setup(bot: commands.Bot):
    await bot.add_cog(PoiCog(bot))

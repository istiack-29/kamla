import discord
from discord.ext import commands
from datetime import datetime, timezone, timedelta
from config import config_manager

POI_TRIGGERS = {"poi", "Poi", "POI"}


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

        content = message.content.strip()

        if content not in POI_TRIGGERS:
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.channel.send(
                    f"⚠️ Only `poi`, `POI`, or `Poi` is allowed in this channel.",
                    delete_after=5,
                )
            except Exception:
                pass
            return

        try:
            await message.delete()
        except Exception:
            pass

        cfg = await config_manager.get_config(message.guild)
        tz_str = cfg.get("timezone", "+00:00")
        tz_offset = _get_tz_offset(tz_str)
        tz = timezone(timedelta(minutes=tz_offset))
        now = datetime.now(tz)
        ts = now.strftime("%H:%M:%S")

        sign = "+" if tz_offset >= 0 else "-"
        abs_offset = abs(tz_offset)
        tz_label = f"GMT{sign}{abs_offset // 60:02d}:{abs_offset % 60:02d}"

        embed = discord.Embed(
            title="🙋 POI Requested",
            description=f"{message.author.mention} has requested a **Point of Information**.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="🕐 Time", value=f"{ts} ({tz_label})", inline=True)
        embed.add_field(name="👤 By", value=message.author.mention, inline=True)
        embed.set_thumbnail(url=message.author.display_avatar.url)

        await message.channel.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(PoiCog(bot))

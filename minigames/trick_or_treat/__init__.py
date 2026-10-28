"""🎃 不给糖就捣蛋 — 万圣夜寻糖：藏糖、公开搜查、找到或被找到即配对。"""
from discord.ext import commands

from .cog import TrickOrTreat
from .views import DYNAMIC_ITEMS


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TrickOrTreat(bot))
    bot.add_dynamic_items(*DYNAMIC_ITEMS)

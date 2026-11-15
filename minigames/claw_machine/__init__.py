"""🧸 万象娃娃机 — 游乐园门票开局、随机荒诞娃娃、隐藏彩蛋、收藏与赠送。"""
from discord.ext import commands

from .cog import ClawMachine
from .views import DYNAMIC_ITEMS


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ClawMachine(bot))
    bot.add_dynamic_items(*DYNAMIC_ITEMS)

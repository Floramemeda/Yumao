"""媚笙阁偷薯饼 — 3–6 人猫鼠隐藏行动小游戏。"""
from discord.ext import commands

from .cog import PotatoHeist
from .views import ActionSelect, GameButton, LobbyButton

DYNAMIC_ITEMS = (LobbyButton, GameButton, ActionSelect)


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(*DYNAMIC_ITEMS)
    await bot.add_cog(PotatoHeist(bot))


async def teardown(bot: commands.Bot) -> None:
    bot.remove_dynamic_items(*DYNAMIC_ITEMS)

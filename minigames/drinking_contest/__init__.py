"""千杯不醉 — 多人回合制喝酒小游戏。"""
from discord.ext import commands

from .cog import DrinkingContest
from .views import ActionButton, LobbyButton


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(LobbyButton, ActionButton)
    await bot.add_cog(DrinkingContest(bot))


async def teardown(bot: commands.Bot) -> None:
    bot.remove_dynamic_items(LobbyButton, ActionButton)

"""超级滚雪球 — 3–6 人合作接力推雪球。"""
from discord.ext import commands

from .cog import SnowballRelay
from .views import ActionButton, CancelButton, LobbyButton

DYNAMIC_ITEMS = (LobbyButton, ActionButton, CancelButton)


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(*DYNAMIC_ITEMS)
    await bot.add_cog(SnowballRelay(bot))


async def teardown(bot: commands.Bot) -> None:
    bot.remove_dynamic_items(*DYNAMIC_ITEMS)

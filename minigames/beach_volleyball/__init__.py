"""沙滩排球 — 2–10 人随机分两队，猜方向、选部位接球，先到目标分的队伍获胜。"""
from discord.ext import commands

from .cog import BeachVolleyball
from .views import AimButton, CancelButton, LobbyButton, ReceiveButton, ServeButton

DYNAMIC_ITEMS = (LobbyButton, ServeButton, AimButton, ReceiveButton, CancelButton)


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(*DYNAMIC_ITEMS)
    await bot.add_cog(BeachVolleyball(bot))


async def teardown(bot: commands.Bot) -> None:
    bot.remove_dynamic_items(*DYNAMIC_ITEMS)

"""撸猫续火花 — 每日撸猫签到、连续火花、火花等级与 22:00 催撸。"""
from discord.ext import commands

from .cog import PetSpark
from .views import PetButton, ReminderToggle

DYNAMIC_ITEMS = (PetButton, ReminderToggle)


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(*DYNAMIC_ITEMS)
    await bot.add_cog(PetSpark(bot))


async def teardown(bot: commands.Bot) -> None:
    bot.remove_dynamic_items(*DYNAMIC_ITEMS)

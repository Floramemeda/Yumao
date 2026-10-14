"""管理计时提醒 — /reminder item days，到期前一天在通知频道 @ 设置人。"""
from discord.ext import commands

from .cog import AdminReminder


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminReminder(bot))

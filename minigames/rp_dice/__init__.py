"""RP 骰点与场次助手 — 自动骰点、/roll、DM 私密控制台与公开状态公告。"""
from discord.ext import commands

from .cog import RpDice


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RpDice(bot))

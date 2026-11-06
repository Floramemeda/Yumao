"""阵营战 — 发一条带两个阵营表情的消息，第一次点表情的人自动加入对应阵营身份组。"""
from discord.ext import commands

from .cog import CampWar


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CampWar(bot))

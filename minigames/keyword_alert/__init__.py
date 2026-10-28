"""论坛关键词提醒 — 指定论坛帖子里出现关键词时 @ 指定身份组。"""
from discord.ext import commands

from .cog import KeywordAlert


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KeywordAlert(bot))

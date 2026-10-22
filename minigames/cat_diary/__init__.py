"""喵咪日事本 — 「记笔记 @造化玉猫 …」写入，/read-diary 公开查询。"""
from discord.ext import commands

from .cog import CatDiary


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CatDiary(bot))

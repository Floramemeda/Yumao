"""皮套系统 — 多 Profile、配额、身份组门槛、Webhook 代发、/send 展示、右键回复、创建日志。"""
from discord.ext import commands

from .cog import PersonaProxy


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PersonaProxy(bot))

"""踩奶 — /踩奶 与右键「踩奶」：生成玉猫猫爪在头像上踩奶的 GIF。"""
from discord.ext import commands

from .cog import PetPet


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PetPet(bot))

"""猫爪 GIF — /踩奶、/戳戳 与右键「踩奶」「戳戳」：玉猫猫爪和头像互动的 GIF。"""
from discord.ext import commands

from .cog import PetPet


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PetPet(bot))

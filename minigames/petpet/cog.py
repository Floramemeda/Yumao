"""踩奶：/踩奶 user 与右键用户 →「踩奶」，生成玉猫猫爪在头像上踩奶的 GIF 公开发出。"""
from __future__ import annotations

import asyncio
import io
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands
from PIL import UnidentifiedImageError

from .render import make_gif

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 5
AVATAR_SIZE = 256
NO_PING = discord.AllowedMentions.none()


def caption(actor_id: int, target_id: int, bot_id: int | None) -> str:
    if target_id == bot_id:
        return f"🐾 玉猫给自己踩了踩奶……？<@{actor_id}> 你在想什么喵！"
    if target_id == actor_id:
        return f"🐾 玉猫给 <@{actor_id}> 踩了踩奶，呼噜呼噜～"
    return f"🐾 玉猫应 <@{actor_id}> 的要求，给 <@{target_id}> 踩了踩奶～"


class PetPet(commands.Cog, name="PetPet"):
    def __init__(self, bot: commands.Bot, clock=time.monotonic):
        self.bot = bot
        self.clock = clock
        self.last_used: dict[int, float] = {}
        self.menu = app_commands.ContextMenu(name="踩奶", callback=self.pet_context)
        self.menu.guild_only = True

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)

    @app_commands.command(name="踩奶", description="让玉猫在某人的头像上踩踩奶（生成 GIF）")
    @app_commands.describe(user="给谁踩奶（不填就踩你自己）")
    @app_commands.guild_only()
    async def pet_command(self, interaction: discord.Interaction, user: discord.User | None = None):
        await self.pet(interaction, user or interaction.user)

    async def pet_context(self, interaction: discord.Interaction, user: discord.User):
        await self.pet(interaction, user)

    async def pet(self, interaction: discord.Interaction, target: discord.abc.User) -> None:
        now = self.clock()
        last = self.last_used.get(interaction.user.id)
        if last is not None and now - last < COOLDOWN_SECONDS:
            wait = int(COOLDOWN_SECONDS - (now - last)) + 1
            return await interaction.response.send_message(f"🐾 猫爪还在半空，{wait} 秒后再踩吧～", ephemeral=True)
        self.last_used[interaction.user.id] = now

        await interaction.response.defer(thinking=True)
        try:
            raw = await target.display_avatar.replace(format="png", size=AVATAR_SIZE).read()
            gif = await asyncio.to_thread(make_gif, raw)
        except (discord.HTTPException, UnidentifiedImageError, OSError) as e:
            log.warning("踩奶：生成失败 user=%s: %s", target.id, e)
            return await interaction.followup.send("😿 玉猫没拿到头像，等一会儿再踩吧。")
        bot_id = self.bot.user.id if self.bot.user else None
        try:
            await interaction.followup.send(caption(interaction.user.id, target.id, bot_id),
                                            file=discord.File(io.BytesIO(gif), filename="petpet.gif"),
                                            allowed_mentions=NO_PING)
        except discord.HTTPException as e:
            log.warning("踩奶：发送失败: %s", e)


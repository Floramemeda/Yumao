"""猫爪 GIF：/踩奶、/戳戳 与右键用户 →「踩奶」「戳戳」，生成玉猫猫爪和对方头像互动的 GIF 公开发出。"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands
from PIL import UnidentifiedImageError

from . import poke, render

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 5       # 两种动作共用冷却
AVATAR_SIZE = 256
NO_PING = discord.AllowedMentions.none()


@dataclass(frozen=True)
class Action:
    name: str                              # 指令 / 右键菜单名
    make_gif: Callable[[bytes], bytes]
    filename: str
    retry: str                             # 冷却 / 失败提示里的动词
    on_bot: str                            # 文案：对玉猫自己
    on_self: str                           # 文案：对自己
    on_other: str                          # 文案：对别人


ACTIONS = {
    "knead": Action("踩奶", render.make_gif, "knead.gif", "踩",
                    "🐾 玉猫给自己踩了踩奶……？{actor} 你在想什么喵！",
                    "🐾 玉猫给 {actor} 踩了踩奶，呼噜呼噜～",
                    "🐾 玉猫应 {actor} 的要求，给 {target} 踩了踩奶～"),
    "poke": Action("戳戳", poke.make_gif, "poke.gif", "戳",
                   "🐾 玉猫戳了戳自己……？{actor} 你在想什么喵！",
                   "🐾 玉猫戳了戳 {actor}，戳一下软一下～",
                   "🐾 玉猫应 {actor} 的要求，戳了戳 {target}～"),
}


def caption(actor_id: int, target_id: int, bot_id: int | None, kind: str = "knead") -> str:
    a = ACTIONS[kind]
    text = a.on_bot if target_id == bot_id else a.on_self if target_id == actor_id else a.on_other
    return text.format(actor=f"<@{actor_id}>", target=f"<@{target_id}>")


class PetPet(commands.Cog, name="PetPet"):
    def __init__(self, bot: commands.Bot, clock=time.monotonic):
        self.bot = bot
        self.clock = clock
        self.last_used: dict[int, float] = {}
        self.menus = []
        for kind, callback in (("knead", self.knead_context), ("poke", self.poke_context)):
            menu = app_commands.ContextMenu(name=ACTIONS[kind].name, callback=callback)
            menu.guild_only = True
            self.menus.append(menu)

    async def cog_load(self) -> None:
        for menu in self.menus:
            self.bot.tree.add_command(menu)

    async def cog_unload(self) -> None:
        for menu in self.menus:
            self.bot.tree.remove_command(menu.name, type=menu.type)

    @app_commands.command(name="踩奶", description="让玉猫在某人的头像上踩踩奶（生成 GIF）")
    @app_commands.describe(user="给谁踩奶（不填就踩你自己）")
    @app_commands.guild_only()
    async def knead_command(self, interaction: discord.Interaction, user: discord.User | None = None):
        await self.pet(interaction, user or interaction.user, "knead")

    @app_commands.command(name="戳戳", description="让玉猫用猫爪戳戳某人的头像（生成 GIF）")
    @app_commands.describe(user="戳谁（不填就戳你自己）")
    @app_commands.guild_only()
    async def poke_command(self, interaction: discord.Interaction, user: discord.User | None = None):
        await self.pet(interaction, user or interaction.user, "poke")

    async def knead_context(self, interaction: discord.Interaction, user: discord.User):
        await self.pet(interaction, user, "knead")

    async def poke_context(self, interaction: discord.Interaction, user: discord.User):
        await self.pet(interaction, user, "poke")

    async def pet(self, interaction: discord.Interaction, target: discord.abc.User, kind: str = "knead") -> None:
        action = ACTIONS[kind]
        now = self.clock()
        last = self.last_used.get(interaction.user.id)
        if last is not None and now - last < COOLDOWN_SECONDS:
            wait = int(COOLDOWN_SECONDS - (now - last)) + 1
            return await interaction.response.send_message(f"🐾 猫爪还在半空，{wait} 秒后再{action.retry}吧～",
                                                           ephemeral=True)
        self.last_used[interaction.user.id] = now

        await interaction.response.defer(thinking=True)
        try:
            raw = await target.display_avatar.replace(format="png", size=AVATAR_SIZE).read()
            gif = await asyncio.to_thread(action.make_gif, raw)
        except (discord.HTTPException, UnidentifiedImageError, OSError) as e:
            log.warning("%s：生成失败 user=%s: %s", action.name, target.id, e)
            return await interaction.followup.send(f"😿 玉猫没拿到头像，等一会儿再{action.retry}吧。")
        bot_id = self.bot.user.id if self.bot.user else None
        try:
            await interaction.followup.send(caption(interaction.user.id, target.id, bot_id, kind),
                                            file=discord.File(io.BytesIO(gif), filename=action.filename),
                                            allowed_mentions=NO_PING)
        except discord.HTTPException as e:
            log.warning("%s：发送失败: %s", action.name, e)

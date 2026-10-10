"""撸猫续火花：Slash Command 与 22:00 催撸定时循环。"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import renderer, views
from .service import PetSparkService

log = logging.getLogger(__name__)


class PetSpark(commands.Cog, name=views.COG_NAME):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.service = PetSparkService(bot)

    async def cog_load(self) -> None:
        self.reminder_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()

    # 每 30 秒检查一次是否到了各 Guild 本地 22:00；真正是否执行由持久化记录决定
    @tasks.loop(seconds=30)
    async def reminder_loop(self):
        await self.service.reminder_tick()

    @reminder_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    @reminder_loop.error
    async def _loop_error(self, error: BaseException):
        log.exception("撸猫火花：催撸循环异常", exc_info=error)

    # ------------------------------------------------------------------ 命令

    @app_commands.command(name="撸猫", description="撸一下玉猫，续上今天的火花")
    @app_commands.guild_only()
    async def pet(self, interaction: discord.Interaction):
        await self.do_pet(interaction)

    @app_commands.command(name="火花", description="查看你的撸猫火花、亲密等级，开关 22:00 催撸")
    @app_commands.guild_only()
    async def spark(self, interaction: discord.Interaction):
        p, today = self.service.status(interaction.guild_id, interaction.user.id)
        reminders_on = not p.reminder_opt_out
        await interaction.response.send_message(
            embed=renderer.status_embed(p, today, self.service.levels, reminders_on),
            view=views.status_view(reminders_on), ephemeral=True)

    # ------------------------------------------------------------------ 共用处理

    async def do_pet(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("要在服务器里才能撸玉猫哦。", ephemeral=True)
            return
        # 先落盘签到，再回复：回复失败也不会导致重复累计
        p, result, today = await self.service.checkin(interaction.guild_id, interaction.user.id)
        content = self.service.checkin_message(interaction.user.id, p, result, today)
        await interaction.response.send_message(content, view=views.pet_view(),
                                                allowed_mentions=discord.AllowedMentions.none())

    async def toggle_reminder(self, interaction: discord.Interaction, opt_out: bool) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("要在服务器里设置哦。", ephemeral=True)
            return
        p = await self.service.set_opt_out(interaction.guild_id, interaction.user.id, opt_out)
        today = self.service.today(interaction.guild_id)
        reminders_on = not p.reminder_opt_out
        embed = renderer.status_embed(p, today, self.service.levels, reminders_on)
        view = views.status_view(reminders_on)
        # 在 /火花 的私密面板上点击时原地更新；其他情况单独回复
        if interaction.message is not None and interaction.message.flags.ephemeral:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

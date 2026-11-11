"""沙滩排球：Slash Command 与事件监听。"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from .service import VolleyballService
from .views import COG_NAME


class BeachVolleyball(commands.Cog, name=COG_NAME):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.service = VolleyballService(bot)
        self._restore_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._restore_task = asyncio.create_task(self.service.restore())

    async def cog_unload(self) -> None:
        if self._restore_task:
            self._restore_task.cancel()
        self.service.shutdown()

    @app_commands.command(name="沙滩排球", description="2–10 人随机分两队打沙滩排球，猜方向、选部位接球！")
    @app_commands.describe(target="先到多少分获胜（10–100，默认 12）")
    @app_commands.rename(target="目标分")
    @app_commands.guild_only()
    async def volleyball(self, interaction: discord.Interaction,
                         target: app_commands.Range[int, 10, 100] | None = None):
        await self.service.create_game(interaction, target)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        await self.service.on_message_deleted(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        for message_id in payload.message_ids:
            await self.service.on_message_deleted(message_id)

"""千杯不醉：Slash Command 与事件监听。"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from .service import DrinkingService
from .views import COG_NAME


class DrinkingContest(commands.Cog, name=COG_NAME):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.service = DrinkingService(bot)
        self._restore_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._restore_task = asyncio.create_task(self.service.restore())

    async def cog_unload(self) -> None:
        if self._restore_task:
            self._restore_task.cancel()
        self.service.shutdown()

    @app_commands.command(name="千杯不醉", description="开一场酒局，最后一个清醒的人获胜！")
    @app_commands.guild_only()
    async def drinking_contest(self, interaction: discord.Interaction):
        await self.service.create_game(interaction)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        await self.service.on_message_deleted(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        for message_id in payload.message_ids:
            await self.service.on_message_deleted(message_id)

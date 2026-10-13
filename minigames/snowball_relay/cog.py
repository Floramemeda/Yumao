"""超级滚雪球：Slash Command 与事件监听。"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from . import renderer
from .service import SnowballService
from .views import COG_NAME


class SnowballRelay(commands.Cog, name=COG_NAME):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.service = SnowballService(bot)
        self._restore_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._restore_task = asyncio.create_task(self.service.restore())

    async def cog_unload(self) -> None:
        if self._restore_task:
            self._restore_task.cancel()
        self.service.shutdown()

    @app_commands.command(name="超级滚雪球", description="3–6 人轮流推同一颗雪球，全队共享得分！")
    @app_commands.guild_only()
    async def snowball(self, interaction: discord.Interaction):
        await self.service.create_game(interaction)

    @app_commands.command(name="雪球排行榜", description="查看本服超级滚雪球个人累计积分榜")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = self.service.leaderboard(interaction.guild_id)
        await interaction.response.send_message(embed=renderer.leaderboard_embed(rows),
                                                allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        await self.service.on_message_deleted(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        for message_id in payload.message_ids:
            await self.service.on_message_deleted(message_id)

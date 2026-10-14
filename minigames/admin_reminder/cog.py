"""管理计时提醒：/reminder 指令与定时检查循环。"""
from __future__ import annotations

import logging
from datetime import date, datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .service import ReminderService

log = logging.getLogger(__name__)

LIST_LIMIT = 20


def list_embed(rows: list[dict]) -> discord.Embed:
    embed = discord.Embed(title="⏰ 待发送的提醒", color=discord.Color.orange())
    if not rows:
        embed.description = "目前没有待发送的提醒。"
        return embed
    lines = []
    for r in rows[:LIST_LIMIT]:
        due = date.fromisoformat(r["due_date"])
        ts = int(datetime.fromisoformat(r["notify_at"]).timestamp())
        lines.append(f"**{discord.utils.escape_markdown(r['item'])}**\n"
                     f"-# 到期 {due.month} 月 {due.day} 日 · 提醒 <t:{ts}:R> · 设置人 <@{r['creator_id']}>")
    if len(rows) > LIST_LIMIT:
        lines.append(f"……以及其他 {len(rows) - LIST_LIMIT} 条")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"共 {len(rows)} 条")
    return embed


class AdminReminder(commands.Cog, name="AdminReminder"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.service = ReminderService(bot)

    async def cog_load(self) -> None:
        self.loop.start()

    async def cog_unload(self) -> None:
        self.loop.cancel()

    @tasks.loop(seconds=30)
    async def loop(self):
        await self.service.tick()

    @loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    @loop.error
    async def _loop_error(self, error: BaseException):
        log.exception("管理提醒：定时检查异常", exc_info=error)

    @app_commands.command(name="reminder", description="（管理）设置到期提醒：到期前一天在通知频道提醒你")
    @app_commands.describe(item="提醒项目（任意文字）", days="几天后到期（整数）")
    @app_commands.guild_only()
    async def reminder(self, interaction: discord.Interaction, item: str,
                       days: app_commands.Range[int, 1, 3650]):
        cfg = self.service.cfg
        if not self.service.is_manager(interaction.user):
            await interaction.response.send_message("只有管理可以设置提醒。", ephemeral=True)
            return
        item = item.strip()
        if not item:
            await interaction.response.send_message("提醒项目不能为空。", ephemeral=True)
            return
        if len(item) > cfg["max_item_length"]:
            await interaction.response.send_message(f"提醒项目最多 {cfg['max_item_length']} 个字。", ephemeral=True)
            return
        if days > cfg["max_days"]:
            await interaction.response.send_message(f"天数最多 {cfg['max_days']} 天。", ephemeral=True)
            return

        r = await self.service.create(interaction.guild_id, interaction.user.id, item, days)
        due = date.fromisoformat(r["due_date"])
        notify_ts = int(datetime.fromisoformat(r["notify_at"]).timestamp())
        await interaction.response.send_message(
            f"✅ 已设置提醒：**{discord.utils.escape_markdown(item)}**\n"
            f"到期日：{due.year} 年 {due.month} 月 {due.day} 日（{days} 天后）\n"
            f"将于 <t:{notify_ts}:F>（<t:{notify_ts}:R>）在 <#{cfg['notify_channel_id']}> 提醒你。",
            ephemeral=True)

    @app_commands.command(name="reminders", description="（管理）查看本服所有待发送的到期提醒")
    @app_commands.guild_only()
    async def reminders(self, interaction: discord.Interaction):
        if not self.service.is_manager(interaction.user):
            await interaction.response.send_message("只有管理可以查看提醒。", ephemeral=True)
            return
        await interaction.response.send_message(embed=list_embed(self.service.pending(interaction.guild_id)),
                                                ephemeral=True)

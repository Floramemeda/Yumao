"""喵咪日事本：「记笔记 @造化玉猫 …」写入监听与 /read-diary 查询。所有回复都公开。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from . import store as st
from .store import DiaryStore
from .text import parse_write

log = logging.getLogger(__name__)

MSG_SAVED = "<@{uid}> 已经帮你记入在喵咪日事上了！你放心！本喵的日事本不会被别人看！！"
MSG_BAD_MENTION = "😾 本喵的日事本不许乱艾特别人！把那些 @ 去掉再来！"
MSG_EMPTY = "📝 要记什么呀？在 @造化玉猫 后面写上内容！"
MSG_TOO_LONG = "📜 太长啦！本喵的日事本一条最多记 {n} 个字。"
MSG_WRITE_COOLDOWN = "⏳ 本喵的爪子还酸着呢，{n} 秒后再来记吧！"
MSG_FULL = "📚 本服的喵咪日事本已经写满了（{n} 条），请联系管理员处理。"
MSG_STORAGE = "😿 本喵的日事本出了点问题，这条没记上，等一会儿再试吧。"
MSG_READ_STORAGE = "😿 本喵的日事本出了点问题，等一会儿再来翻吧。"
MSG_DISABLED = "📕 本服的喵咪日事本还没开放。"
MSG_READ_COOLDOWN = "⏳ 本喵刚翻过日事本，{n} 秒后再来！"
MSG_EMPTY_QUERY = "🙀 关键词不能只有空格！"
MSG_QUERY_TOO_LONG = "🙀 关键词最多 {n} 个字！"
MSG_NOTHING = "🐱 本喵翻遍了日事本，什么也没找到！"

EMBED_BUDGET = 5500   # 单条消息所有 Embed 字符合计上限 6000，留余量
NO_MENTIONS = discord.AllowedMentions.none()


def load_config() -> dict:
    with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
        return json.load(f)


def safe_text(s: str) -> str:
    """展示用：转义 Markdown（粗斜体、链接、引用、标题、列表等），原文不改。"""
    return discord.utils.escape_markdown(s)


def render_results(entries: list[dict], names: dict[str, str], target_user_id: int | None) -> list[dict]:
    """把至多 N 条结果渲染成一条或多条公开消息（kwargs 列表），Embed 超限时拆分。"""
    if not entries:
        return [{"content": MSG_NOTHING}]
    embeds = []
    for e in entries:
        embed = discord.Embed(description=safe_text(e["content"]), color=discord.Color.from_rgb(255, 200, 120),
                              timestamp=datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")))
        if target_user_id is None:
            embed.set_author(name=f"{names[e['user_id']]} 记录了：")
        embeds.append(embed)

    messages, batch, size = [], [], 0
    for embed in embeds:
        n = len(embed)
        if batch and (size + n > EMBED_BUDGET or len(batch) >= 10):
            messages.append({"embeds": batch})
            batch, size = [], 0
        batch.append(embed)
        size += n
    messages.append({"embeds": batch})
    if target_user_id is not None:
        messages[0]["content"] = f"📔 **{safe_text(names[str(target_user_id)])} 记录了：**"
    return messages


class CatDiary(commands.Cog, name="CatDiary"):
    def __init__(self, bot: commands.Bot, cfg: dict | None = None, store: DiaryStore | None = None):
        self.bot = bot
        self.cfg = cfg or load_config()
        self.store = store or DiaryStore(self.cfg)

    def guild_cfg(self, guild_id: int | None) -> dict | None:
        g = self.cfg.get("guilds", {}).get(str(guild_id))
        return g if g and g.get("enabled") else None

    # ------------------------------------------------------------------ 写入

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        gcfg = self.guild_cfg(message.guild.id)
        if gcfg is None or message.channel.id != int(gcfg.get("write_channel_id") or 0):
            return
        bot_id = self.bot.user.id
        parsed = parse_write(message.content, bot_id)
        if parsed is None:
            return

        others = any(u.id != bot_id for u in message.mentions)
        if (parsed.bad_mention or others or message.role_mentions or message.channel_mentions
                or message.mention_everyone):
            return await self._reply(message, MSG_BAD_MENTION)
        if not parsed.body:
            return await self._reply(message, MSG_EMPTY)
        if len(parsed.body) > self.cfg["max_entry_length"]:
            return await self._reply(message, MSG_TOO_LONG.format(n=self.cfg["max_entry_length"]))

        result = await self.store.add(message.guild.id, message.author.id, message.channel.id, message.id,
                                      parsed.body, message.created_at)
        if result.status == st.OK:
            await self._reply(message, MSG_SAVED.format(uid=message.author.id), ping=message.author.id)
        elif result.status == st.COOLDOWN:
            await self._reply(message, MSG_WRITE_COOLDOWN.format(n=result.retry_after))
        elif result.status == st.FULL:
            await self._reply(message, MSG_FULL.format(n=self.cfg["max_entries_per_guild"]))
        elif result.status == st.STORAGE_ERROR:
            await self._reply(message, MSG_STORAGE)
        # DUPLICATE：同一条消息重复投递，静默忽略

    async def _reply(self, message: discord.Message, text: str, ping: int | None = None) -> None:
        allowed = (discord.AllowedMentions(everyone=False, roles=False, replied_user=False,
                                           users=[discord.Object(ping)]) if ping else NO_MENTIONS)
        try:
            await message.reply(text, mention_author=False, allowed_mentions=allowed)
        except discord.HTTPException as e:
            log.warning("喵咪日事本：回复失败 guild=%s: %s", message.guild.id, e)

    # ------------------------------------------------------------------ 查询

    @app_commands.command(name="read-diary", description="公开翻看本服喵咪日事本最新五条（所有人可见）")
    @app_commands.describe(user="只看这个人的笔记", text="关键词（繁简通用）")
    @app_commands.guild_only()
    async def read_diary(self, interaction: discord.Interaction,
                         user: discord.User | None = None, text: str | None = None):
        await interaction.response.defer(thinking=True)   # 公开 defer，结果公开发出
        if self.guild_cfg(interaction.guild_id) is None:
            return await self._followup(interaction, {"content": MSG_DISABLED})
        if text is not None:
            text = text.strip()
            if not text:
                return await self._followup(interaction, {"content": MSG_EMPTY_QUERY})
            if len(text) > self.cfg["max_query_length"]:
                return await self._followup(interaction,
                                            {"content": MSG_QUERY_TOO_LONG.format(n=self.cfg["max_query_length"])})

        res = await self.store.search(interaction.guild_id, interaction.user.id, user.id if user else None, text)
        if res.status == st.COOLDOWN:
            return await self._followup(interaction, {"content": MSG_READ_COOLDOWN.format(n=res.retry_after)})
        if res.status == st.STORAGE_ERROR:
            return await self._followup(interaction, {"content": MSG_READ_STORAGE})

        ids = {e["user_id"] for e in res.entries} | ({str(user.id)} if user else set())
        names = {uid: await self._display_name(interaction.guild, int(uid)) for uid in ids}
        for msg in render_results(res.entries, names, user.id if user else None):
            await self._followup(interaction, msg)

    async def _followup(self, interaction: discord.Interaction, kwargs: dict) -> None:
        try:
            await interaction.followup.send(**kwargs, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            log.warning("喵咪日事本：查询结果发送失败 guild=%s: %s", interaction.guild_id, e)

    async def _display_name(self, guild, user_id: int) -> str:
        """作者展示名只用于显示；归属永远按存储的 user_id。退服用户回退到用户名或 ID。"""
        member = guild.get_member(user_id) if guild else None
        if member is not None:
            return member.display_name
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                user = None
        return user.display_name if user else f"用户{user_id}"

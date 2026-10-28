"""论坛关键词提醒：监控指定论坛频道下所有帖子，消息出现关键词时回复并 @ 指定身份组。

匹配前做 NFKC + 繁转简 + 忽略大小写 + 去掉空白，所以「私聊」「私 聊」「ＳＩ聊」这类变体也能识别；原消息不改。
同一帖子在冷却时间内只提醒一次，避免连发关键词刷屏 @ 管理员。
"""
from __future__ import annotations

import collections
import json
import logging
import re
import time
from pathlib import Path

import discord
from discord.ext import commands

from ..cat_diary.text import to_simplified
from ..persona_proxy.webhook_sender import is_persona_webhook

log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
        return json.load(f)


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", to_simplified(text or "")).casefold()


def find_keyword(text: str, keywords: list[str]) -> str | None:
    """返回第一个命中的关键词（按配置原样），没有命中返回 None。"""
    norm = normalize(text)
    for kw in keywords:
        if normalize(kw) in norm:
            return kw
    return None


class KeywordAlert(commands.Cog, name="KeywordAlert"):
    def __init__(self, bot: commands.Bot, cfg: dict | None = None, clock=time.monotonic):
        self.bot = bot
        self.cfg = cfg or load_config()
        self.clock = clock
        self.last_alert: dict[int, float] = {}               # thread id -> 上次提醒时间
        self.seen = collections.deque(maxlen=1000)           # 最近处理过的消息 id

    def watch_for(self, message: discord.Message) -> dict | None:
        """消息所在帖子的父频道是被监控的论坛时返回对应配置。"""
        if message.guild is None:
            return None
        parent_id = getattr(message.channel, "parent_id", None)
        for w in self.cfg["watches"]:
            if int(w["guild_id"]) == message.guild.id and parent_id in {int(c) for c in w["forum_channel_ids"]}:
                return w
        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return
        # 其他机器人 / Webhook 不算；玉猫皮套背后是真人，照常检查
        if (message.author.bot or message.webhook_id is not None) and not is_persona_webhook(message.webhook_id):
            return
        watch = self.watch_for(message)
        if watch is None:
            return
        keyword = find_keyword(message.content, watch["keywords"])
        if keyword is None or message.id in self.seen:
            return
        self.seen.append(message.id)

        now = self.clock()
        thread_id = message.channel.id
        if now - self.last_alert.get(thread_id, -1e9) < self.cfg.get("cooldown_seconds_per_thread", 0):
            return
        self.last_alert[thread_id] = now

        role_id = int(watch["alert_role_id"])
        try:
            await message.reply(f"<@&{role_id}> ⚠️ 检测到关键词「{discord.utils.escape_markdown(keyword)}」",
                                mention_author=False,
                                allowed_mentions=discord.AllowedMentions(everyone=False, users=False,
                                                                         replied_user=False,
                                                                         roles=[discord.Object(role_id)]))
        except discord.HTTPException as e:
            log.warning("关键词提醒：回复失败 thread=%s: %s", thread_id, e)

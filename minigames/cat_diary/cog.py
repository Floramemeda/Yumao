"""喵咪日事本：「记笔记 @造化玉猫 …」写入监听、右键消息「记笔记」与 /read-diary 查询。写入与查询结果都公开。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from . import store as st
from .store import DiaryStore
from ..persona_proxy.webhook_sender import is_persona_webhook
from .text import parse_write, strip_markdown

log = logging.getLogger(__name__)

MSG_SAVED = "<@{uid}> 已经帮你记入在喵咪日事上了！你放心！本喵的日事本不会被别人看！！"
MSG_NOTED = "<@{uid}> 已经帮你把 {speaker} 的这句话记入喵咪日事本了！你放心！本喵的日事本不会被别人看！！"
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


DESC_LIMIT = 4000     # 单个 Embed 描述上限 4096，留余量


def result_title(names: dict[str, str], target_user_id: int | None, text: str | None) -> str:
    parts = []
    if target_user_id is not None:
        parts.append(names[str(target_user_id)])
    if text:
        parts.append(text)
    return f"📔【{'/'.join(parts) or '最新笔记'}】"


def render_results(entries: list[dict], names: dict[str, str], target_user_id: int | None,
                   text: str | None = None) -> list[dict]:
    """结果合并成一个 Embed：标题【某人/关键字】，每条笔记一行「* 」。

    只有内容超过单个 Embed 上限时才接续到下一个 Embed（同一条消息内，必要时再拆消息），从不截断。
    """
    if not entries:
        return [{"content": MSG_NOTHING}]
    color = discord.Color.from_rgb(255, 200, 120)
    bullets = []
    for e in entries:
        body = safe_text(e["content"]).replace("\n", "\n  ")   # 多行笔记缩进，留在同一个列表项里
        if target_user_id is None:
            speaker = e.get("persona_name") or names[e["user_id"]]
            body = f"**{safe_text(speaker)}**：{body}"
        bullets.append(f"* {body}")

    embeds, current = [], ""
    for b in bullets:
        if current and len(current) + 1 + len(b) > DESC_LIMIT:
            embeds.append(discord.Embed(description=current, color=color))
            current = b
        else:
            current = f"{current}\n{b}" if current else b
    embeds.append(discord.Embed(description=current, color=color))
    embeds[0].title = result_title(names, target_user_id, text)

    messages, batch, size = [], [], 0
    for embed in embeds:
        n = len(embed)
        if batch and (size + n > EMBED_BUDGET or len(batch) >= 10):
            messages.append({"embeds": batch})
            batch, size = [], 0
        batch.append(embed)
        size += n
    messages.append({"embeds": batch})
    return messages


class CatDiary(commands.Cog, name="CatDiary"):
    def __init__(self, bot: commands.Bot, cfg: dict | None = None, store: DiaryStore | None = None):
        self.bot = bot
        self.cfg = cfg or load_config()
        self.store = store or DiaryStore(self.cfg)
        self.note_menu = app_commands.ContextMenu(name="记笔记", callback=self.note_context)
        self.note_menu.guild_only = True

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.note_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.note_menu.name, type=self.note_menu.type)

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
        body = strip_markdown(parsed.body)              # 只记文字本身
        if not body:
            return await self._reply(message, MSG_EMPTY)
        if len(body) > self.cfg["max_entry_length"]:
            return await self._reply(message, MSG_TOO_LONG.format(n=self.cfg["max_entry_length"]))

        result = await self.store.add(message.guild.id, message.author.id, message.channel.id, message.id,
                                      body, message.created_at)
        if result.status == st.OK:
            await self._reply(message, MSG_SAVED.format(uid=message.author.id), ping=message.author.id)
        elif result.status == st.COOLDOWN:
            await self._reply(message, MSG_WRITE_COOLDOWN.format(n=result.retry_after))
        elif result.status == st.FULL:
            await self._reply(message, MSG_FULL.format(n=self.cfg["max_entries_per_guild"]))
        elif result.status == st.STORAGE_ERROR:
            await self._reply(message, MSG_STORAGE)
        # DUPLICATE：同一条消息重复投递，静默忽略

    async def note_context(self, interaction: discord.Interaction, message: discord.Message):
        """右键消息 →「记笔记」：把这条消息记入日事本，说话人是原消息作者；玉猫回复原消息并 @ 点击的人。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        gcfg = self.guild_cfg(interaction.guild_id)
        if gcfg is None or message.channel.id != int(gcfg.get("write_channel_id") or 0):
            return await self._private(interaction, "📕 只能在本服的记笔记频道里用「记笔记」哦。")
        # 皮套消息记皮套名字（不暴露背后的真人）；其他机器人 / Webhook 不记
        persona = message.author.name if is_persona_webhook(message.webhook_id) else None
        if persona is None and (message.author.bot or message.webhook_id is not None):
            return await self._private(interaction, "🙀 本喵只记真人和皮套说的话，机器人的消息不能记。")
        body = strip_markdown(message.clean_content)   # 提及转成纯文字名字；去掉 Markdown 只留文字本身
        if not body:
            return await self._private(interaction, "📝 这条消息没有文字，本喵记不了。")
        if len(body) > self.cfg["max_entry_length"]:
            return await self._private(interaction, MSG_TOO_LONG.format(n=self.cfg["max_entry_length"]))

        clicker = interaction.user.id
        author_id = None if persona else message.author.id
        recorded_by = clicker if clicker != author_id else None
        result = await self.store.add(interaction.guild_id, author_id, message.channel.id, message.id,
                                      body, message.created_at, recorded_by=recorded_by, persona_name=persona)
        if result.status == st.OK:
            log.info("喵咪日事本：guild=%s 右键记笔记 message=%s", interaction.guild_id, message.id)
            speaker = f"**{safe_text(persona)}**" if persona else f"<@{author_id}>"
            text = MSG_NOTED.format(uid=clicker, speaker=speaker) if recorded_by else MSG_SAVED.format(uid=clicker)
            await self._reply(message, text, ping=clicker)
            return await self._private(interaction, "✅ 记好啦！")
        await self._private(interaction, {
            st.DUPLICATE: "📔 这条消息已经记在日事本里了。",
            st.COOLDOWN: MSG_WRITE_COOLDOWN.format(n=result.retry_after),
            st.FULL: MSG_FULL.format(n=self.cfg["max_entries_per_guild"]),
        }.get(result.status, MSG_STORAGE))

    async def _private(self, interaction: discord.Interaction, text: str) -> None:
        try:
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            log.warning("喵咪日事本：私密回复失败 guild=%s: %s", interaction.guild_id, e)

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

        ids = {e["user_id"] for e in res.entries if e["user_id"]} | ({str(user.id)} if user else set())
        names = {uid: await self._display_name(interaction.guild, int(uid)) for uid in ids}
        for msg in render_results(res.entries, names, user.id if user else None, text):
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

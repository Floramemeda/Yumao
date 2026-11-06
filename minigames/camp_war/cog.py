"""阵营战：管理员用 /阵营战 在指定频道发一条阵营选择消息，玉猫先点上两个阵营表情。

玩家第一次点其中一个表情时，自动加上对应阵营的身份组，那个表情保留（算作一票）。
已经有阵营的人：再点别的阵营表情、或在这条消息上点任何其他表情，都会被玉猫撤掉，身份组不变；
取消自己的表情也不会移除身份组（阵营一经选定不可更改）。

表情可以来自玉猫所在的任何服务器（如云境测试服）：按 config.json 的 emoji_id 找，没有 id 时按 emoji 名字找；
点表情时也按 id 判断（没有 id 时按名字）。
已发出的阵营消息 id 存在 data_file 里，Bot 重启后照常生效。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from ..common import PROJECT_ROOT, is_admin, write_json_atomic

log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
        return json.load(f)


def matches(camp: dict, emoji) -> bool:
    if camp.get("emoji_id"):
        return emoji.id == int(camp["emoji_id"])
    return emoji.name == camp["emoji"]


def camp_text(title: str, camps: list[dict], emojis: list) -> str:
    lines = [f"{e} **{c['label']}**" + (f" — {c['description']}" if c.get("description") else "")
             for c, e in zip(camps, emojis)]
    return f"{title}\n\n" + "\n".join(lines)


class CampWar(commands.Cog, name="CampWar"):
    def __init__(self, bot: commands.Bot, cfg: dict | None = None, data_file: Path | None = None):
        self.bot = bot
        self.cfg = cfg or load_config()
        self.data_file = data_file or PROJECT_ROOT / self.cfg["data_file"]
        self.messages: dict[str, str] = self._load()     # 阵营消息 id -> 服务器 id
        self.claimed: dict[tuple[int, int], int] = {}     # (服务器, 用户) -> 本进程刚加上的阵营身份组（缓存未更新前防连点）
        self.lock = asyncio.Lock()

    def _load(self) -> dict[str, str]:
        try:
            with open(self.data_file, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def guild_cfg(self, guild_id: int) -> dict | None:
        return self.cfg["guilds"].get(str(guild_id))

    def find_emoji(self, camp: dict):
        if camp.get("emoji_id"):
            return self.bot.get_emoji(int(camp["emoji_id"]))
        return discord.utils.get(self.bot.emojis, name=camp["emoji"])

    # ------------------------------------------------------------------ /阵营战

    @app_commands.command(name="阵营战", description="（管理）发起阵营战：发一条阵营选择消息，点表情即加入阵营")
    @app_commands.describe(title="消息开头的文字（默认「道友选择阵营：」）")
    @app_commands.guild_only()
    async def camp_war(self, interaction: discord.Interaction, title: str | None = None):
        if not is_admin(interaction):
            return await interaction.response.send_message("只有管理员可以发起阵营战。", ephemeral=True)
        gcfg = self.guild_cfg(interaction.guild_id)
        if gcfg is None:
            return await interaction.response.send_message("这个服务器还没有配置阵营战。", ephemeral=True)
        camps = gcfg["camps"]
        emojis = [self.find_emoji(c) for c in camps]
        missing = [c["emoji"] for c, e in zip(camps, emojis) if e is None]
        if missing:
            return await interaction.response.send_message(
                f"找不到表情：{'、'.join(missing)}（玉猫需要在表情所在的服务器里）。", ephemeral=True)
        channel = interaction.guild.get_channel(int(gcfg["channel_id"]))
        if channel is None:
            return await interaction.response.send_message("找不到阵营战频道。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = discord.Embed(description=camp_text(title or self.cfg["default_title"], camps, emojis))
        try:
            msg = await channel.send(embed=embed)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"发送失败：{e}", ephemeral=True)
        self.messages[str(msg.id)] = str(interaction.guild_id)
        write_json_atomic(self.data_file, self.messages)
        try:
            for e in emojis:
                await msg.add_reaction(e)
        except discord.HTTPException as e:
            log.warning("阵营战：添加表情失败 message=%s: %s", msg.id, e)
        await interaction.followup.send(f"阵营战已发起：{msg.jump_url}", ephemeral=True)

    # ------------------------------------------------------------------ 点表情

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if str(payload.message_id) not in self.messages or payload.guild_id is None:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return
        gcfg = self.guild_cfg(payload.guild_id)
        guild = self.bot.get_guild(payload.guild_id)
        member = payload.member
        if gcfg is None or guild is None or member is None or member.bot:
            return
        camps = gcfg["camps"]
        camp_roles = {int(c["role_id"]) for c in camps}
        picked = next((c for c in camps if matches(c, payload.emoji)), None)
        async with self.lock:
            key = (guild.id, member.id)
            current = next((r.id for r in member.roles if r.id in camp_roles), None) or self.claimed.get(key)
            if picked is not None and current is None:          # 第一次选：加身份组，表情保留
                role = guild.get_role(int(picked["role_id"]))
                if role is None:
                    log.warning("阵营战：找不到身份组 %s", picked["role_id"])
                    return
                try:
                    await member.add_roles(role, reason="阵营战：选择阵营")
                except discord.HTTPException as e:
                    log.warning("阵营战：加身份组失败 user=%s: %s", member.id, e)
                    return await self._remove(payload, member)
                self.claimed[key] = role.id
                return
            if picked is not None and current == int(picked["role_id"]):
                return                                          # 点回自己阵营的表情：保留
        await self._remove(payload, member)                     # 已有阵营再点别的 / 无关表情：撤掉

    async def _remove(self, payload: discord.RawReactionActionEvent, member: discord.Member) -> None:
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            await channel.get_partial_message(payload.message_id).remove_reaction(payload.emoji, member)
        except discord.HTTPException as e:
            log.warning("阵营战：撤表情失败 message=%s: %s", payload.message_id, e)

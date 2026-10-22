"""皮套系统：展示文案、Embed 与选项标签。"""
from __future__ import annotations

from datetime import datetime

import discord

COLOR = discord.Color.from_rgb(180, 140, 220)
KIND_LABELS = {"say": "发言", "reply": "回复", "showcase": "展示"}
STATUS_LABELS = {"pending": "待发送", "failed": "发送失败", "partial_success": "只发出了第一条",
                 "unknown": "结果不明", "sending": "发送中", "sent": "已发出"}


def short_id(pid: str) -> str:
    return pid.split("_", 1)[-1][:6]


def profile_label(p: dict, disabled: bool = False) -> str:
    label = f"{p['name']} · {short_id(p['id'])}" + ("（停用）" if disabled else "")
    return label[:100]


def reply_content(jump_url: str, text: str) -> str:
    """V1 不做原生回复引用：在正文前加原消息跳转链接。"""
    return f"-# ↪️ 回复 {jump_url}\n{text}"


def draft_label(d: dict, profile_name: str) -> str:
    when = d["created_at"][5:16].replace("T", " ")
    snippet = " ".join(d["text"].split())[:30]
    return f"[{STATUS_LABELS.get(d['status'], d['status'])}] {KIND_LABELS.get(d['kind'], d['kind'])}·{profile_name} {when} {snippet}"[:100]


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def profile_list_embed(profiles: list[dict], usable_ids: set[str], limit: int, has_role: bool) -> discord.Embed:
    embed = discord.Embed(title="🎭 我的皮套", color=COLOR)
    if not profiles:
        embed.description = "你还没有皮套，用 `/profile create` 创建一个吧。"
    else:
        lines = []
        for p in profiles:
            state = "✅ 可用" if p["id"] in usable_ids else "⏸️ 超出配额，暂时停用"
            lines.append(f"**{discord.utils.escape_markdown(p['name'])}** · `{p['id']}` · {state}")
        embed.description = "\n".join(lines)
    embed.add_field(name="配额", value=f"{len(profiles)} / {limit}", inline=True)
    embed.add_field(name="身份组", value="✅ 可以使用皮套" if has_role else "❌ 没有准入身份组", inline=True)
    return embed


def creation_log_embed(p: dict, owner_id: int, count: int, limit: int) -> discord.Embed:
    embed = discord.Embed(title="🎭 新皮套创建", color=COLOR)
    embed.add_field(name="创建者", value=f"<@{owner_id}>（{owner_id}）", inline=False)
    embed.add_field(name="角色名", value=discord.utils.escape_markdown(p["name"]), inline=True)
    embed.add_field(name="Profile ID", value=f"`{p['id']}`", inline=True)
    embed.add_field(name="配额", value=f"{count} / {limit}", inline=True)
    embed.add_field(name="创建时间", value=f"<t:{_ts(p['created_at'])}:F>", inline=False)
    if p.get("avatar_url"):
        embed.set_thumbnail(url=p["avatar_url"])
    return embed


def draft_embed(d: dict, profile_name: str) -> discord.Embed:
    embed = discord.Embed(title=f"📝 草稿 {d['id']}", color=COLOR,
                          description=f"```\n{d['text'].replace('```', '`​`​`')}\n```")
    embed.add_field(name="状态", value=STATUS_LABELS.get(d["status"], d["status"]), inline=True)
    embed.add_field(name="皮套", value=discord.utils.escape_markdown(profile_name), inline=True)
    embed.add_field(name="频道", value=f"<#{d['channel_id']}>", inline=True)
    if d.get("error"):
        embed.add_field(name="上次错误", value=d["error"][:1000], inline=False)
    return embed


def audit_text(rec: dict) -> str:
    lines = [f"真实发送者：<@{rec['owner_id']}>（{rec['owner_id']}）",
             f"皮套：{discord.utils.escape_markdown(rec.get('profile_name', ''))} · `{rec['profile_id']}`",
             f"类型：{KIND_LABELS.get(rec.get('kind'), rec.get('kind'))} · 草稿 `{rec.get('draft_id')}`",
             f"时间：<t:{_ts(rec['at'])}:F>"]
    if rec.get("reply_to"):
        lines.append(f"回复的原消息：{rec['reply_to'].get('jump_url')}")
    return "\n".join(lines)

"""RP 场次：公告、状态面板与 DM 控制台的展示。公告只基于已提交的数据生成。"""
from __future__ import annotations

import discord

from .service import STATS

COLOR = discord.Color.from_rgb(120, 110, 200)
FIELD_LIMIT = 1000


def _m(uid) -> str:
    return f"<@{uid}>"


def progress_bar(cur: int, total: int, width: int = 10) -> str:
    filled = round(cur / total * width) if total else 0
    return "▰" * filled + "▱" * (width - filled)


def conditions_text(p: dict) -> str:
    return "、".join(discord.utils.escape_markdown(c["name"]) for c in p["conditions"]) or "无"


def player_line(uid: str, p: dict) -> str:
    return f"{_m(uid)} 理智 **{p['sanity']}** · 发情值 **{p['heat']}** · 异常：{conditions_text(p)}"


def _signed(n: int) -> str:
    return f"+{n}" if n >= 0 else str(n)


def announcement(session: dict, ann: dict) -> str:
    kind = ann["kind"]
    if kind == "progress":
        return (f"🧭 探索进度：{ann['before']}/{ann['max']} → {ann['after']}/{ann['max']}"
                f"（DM：{_m(ann['actor'])}）")
    if kind == "end":
        return (f"🏁 探索结束！最终进度 {session['progress']}/{session['progress_max']}"
                f"（操作者：{_m(ann['actor'])}）")
    # 玩家状态：变化的数值显示前后与实际差值，未变的显示当前值
    p = session["participants"][ann["user_id"]]
    lines = [f"📋 探索者状态更新 — {_m(ann['user_id'])}"]
    for key, label in STATS.items():
        if key in ann["changes"]:
            before, after = ann["changes"][key]
            lines.append(f"{label}：{before} → {after}（{_signed(after - before)}）")
        else:
            lines.append(f"{label}：{p[key]}")
    cond = conditions_text(p)
    if ann.get("condition_change"):
        sign, name = ann["condition_change"]
        mark = "新增" if sign == "+" else "解除"
        cond += f"（{mark}：{discord.utils.escape_markdown(name)}）"
    lines.append(f"异常状态：{cond}")
    lines.append(f"操作者：{_m(ann['actor'])}")
    return "\n".join(lines)


def start_message(session: dict) -> str:
    return (f"🗺️ 探索开始！DM：{_m(session['dm_user_id'])} · 进度 0/{session['progress_max']}\n"
            f"-# 用 `/rp status` 查看队伍状态；DM 用 `/rp dm` 打开控制台。")


def _player_chunks(session: dict, players) -> list[str]:
    chunks, cur = [], ""
    for uid, p in players:
        line = player_line(uid, p)
        if cur and len(cur) + 1 + len(line) > FIELD_LIMIT:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def status_embeds(session: dict, players, title: str = "🗺️ 探索状态") -> list[discord.Embed]:
    """公开状态：共享进度 + 每位在场玩家；玩家多时拆成多个 Embed。"""
    head = discord.Embed(title=title, color=COLOR)
    head.add_field(name="DM", value=_m(session["dm_user_id"]), inline=True)
    head.add_field(name="探索进度", value=f"{progress_bar(session['progress'], session['progress_max'])} "
                                         f"{session['progress']}/{session['progress_max']}", inline=True)
    chunks = _player_chunks(session, players)
    if not chunks:
        head.add_field(name="玩家", value="还没有玩家加入。", inline=False)
        return [head]
    embeds, cur = [head], head
    for i, chunk in enumerate(chunks):
        if len(cur.fields) >= 20 or len(cur) + len(chunk) > 5000:
            cur = discord.Embed(color=COLOR)
            embeds.append(cur)
        cur.add_field(name="玩家" if i == 0 else "​", value=chunk, inline=False)
    return embeds


def dm_panel_embed(session: dict, players, note: str | None = None) -> discord.Embed:
    embed = status_embeds(session, players, title="🎛️ DM 控制台（仅你可见）")[0]
    if note:
        embed.description = note
    embed.set_footer(text="面板过期或 Bot 重启后，再用 /rp dm 打开即可")
    return embed


def player_panel_embed(uid: str, p: dict, note: str | None = None) -> discord.Embed:
    embed = discord.Embed(title="🎛️ 玩家操作（仅你可见）", description=note or "", color=COLOR)
    embed.add_field(name="玩家", value=_m(uid), inline=False)
    for key, label in STATS.items():
        embed.add_field(name=label, value=str(p[key]), inline=True)
    embed.add_field(name="异常状态", value=conditions_text(p), inline=False)
    return embed

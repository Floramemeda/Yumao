"""万象娃娃机：所有 Embed。抓到之前绝不显示异常特征和彩蛋；成功率只在服务端使用，不展示给玩家。"""
from __future__ import annotations

from datetime import datetime, timezone

import discord

TITLE = "🎟️ 南域游乐场 · 万象娃娃机"
COLOR_GAME = discord.Color.from_rgb(255, 170, 200)
COLOR_WIN = discord.Color.from_rgb(120, 200, 140)
COLOR_EGG = discord.Color.gold()
COLOR_FAIL = discord.Color.dark_grey()
COLOR_INFO = discord.Color.blurple()
NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def _m(uid) -> str:
    return f"<@{uid}>"


def clue_lines(cfg: dict, cand: dict) -> str:
    g, p = cfg["grips"][cand["grip"]], cfg["positions"][cand["position"]]
    return f"　✋ 抓取点：**{g['name']}** —— {g['hint']}\n　📍 位置：**{p['name']}** —— {p['hint']}"


def candidate_name(cand: dict) -> str:
    n = cand["names"]
    return f"{n['mood']}{n['subject']}{n['toy']}"


def pick_embed(cfg: dict, game: dict, others: int = 0) -> discord.Embed:
    lines = [f"{_m(game['user_id'])} 投进了一张门票，机器里晃着三只娃娃。\n先观察，再选一只下爪（每局只能抓一次）：", ""]
    for i, cand in enumerate(game["candidates"]):
        lines += [f"{NUMS[i]} **{candidate_name(cand)}**", clue_lines(cfg, cand), ""]
    if others:
        lines.append(f"-# 你还有 {others} 局待玩，玩完这局用 `/claw resume` 继续。")
    embed = discord.Embed(title=TITLE, description="\n".join(lines).strip(), color=COLOR_GAME)
    embed.set_footer(text=f"第 {game['id']} 局")
    return embed


def technique_text(cfg: dict) -> str:
    return "\n".join(f"{t['emoji']} **{t['name']}**：{t['hint']}" for t in cfg["techniques"].values())


def aim_embed(cfg: dict, game: dict, idx: int, technique: str | None = None) -> discord.Embed:
    cand = game["candidates"][idx]
    lines = [f"目标：{NUMS[idx]} **{candidate_name(cand)}**", clue_lines(cfg, cand), "", technique_text(cfg)]
    if technique:
        t = cfg["techniques"][technique]
        lines += ["", f"已选：{t['emoji']} **{t['name']}**。确认后夹爪就落下去了，只有一次机会！"]
    else:
        lines += ["", "选一种抓法："]
    embed = discord.Embed(title=TITLE, description="\n".join(lines), color=COLOR_GAME)
    embed.set_footer(text=f"第 {game['id']} 局")
    return embed


def doll_block(doll: dict) -> list[str]:
    lines = [f"🧸 **{doll['name']}**", "", "异常特征：", doll["names"]["quirk"]]
    if doll["egg"]:
        lines += ["", f"✨ 隐藏彩蛋：**{doll['egg']['title']}**"]
        if doll["egg"].get("story"):
            lines.append(f"-# {doll['egg']['story']}")
    return lines


def result_embed(cfg: dict, game: dict, doll: dict | None) -> discord.Embed:
    r = game["result"]
    cand = game["candidates"][r["pick"]]
    tech = cfg["techniques"].get(r["technique"], {}).get("name", r["technique"])
    if not r["success"]:
        text = (f"{_m(game['user_id'])} 用 **{tech}** 抓 **{candidate_name(cand)}**……\n\n"
                f"💨 {r['fail_text']}\n\n这局没抓到，下次再来！")
        embed = discord.Embed(title="😿 抓取失败", description=text, color=COLOR_FAIL)
    else:
        lines = [f"{_m(game['user_id'])} 用 **{tech}** 一爪命中！", ""] + doll_block(doll) + [
            "", f"娃娃编号：`{doll['id']}`",
            f"原始抓取者：{_m(doll['original_owner_id'])}",
            f"当前持有者：{_m(doll['original_owner_id'])}", "", "已自动收入收藏柜！"]
        embed = discord.Embed(title="🎉 抓取成功！", description="\n".join(lines),
                              color=COLOR_EGG if doll["egg"] else COLOR_WIN)
    embed.set_footer(text=f"第 {game['id']} 局")
    return embed


def detail_embed(doll: dict) -> discord.Embed:
    lines = doll_block(doll) + [
        "", f"娃娃编号：`{doll['id']}`",
        f"原始抓取者：{_m(doll['original_owner_id'])}",
        f"当前持有者：{_m(doll['current_owner_id'])}",
        f"抓获于：<t:{_ts(doll['caught_at'])}:f>",
    ]
    if doll["is_showcased"]:
        lines.append("🏆 正在展示")
    if doll["pending_gift"]:
        lines.append("🎁 有一份待对方确认的赠礼，暂时锁定")
    return discord.Embed(title="🧸 娃娃详情", description="\n".join(lines),
                         color=COLOR_EGG if doll["egg"] else COLOR_INFO)


def _ts(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


FILTER_LABELS = {"all": "全部", "eggs": "隐藏彩蛋", "showcase": "展示中"}


def short_line(doll: dict) -> str:
    marks = ("🏆" if doll["is_showcased"] else "") + ("✨" if doll["egg"] else "") + ("🎁" if doll["pending_gift"] else "")
    return f"`{doll['id']}` {doll['name']} {marks}".rstrip()


def collection_embed(user_id: int, dolls: list[dict], flt: str, page: int, pages: int, total: int) -> discord.Embed:
    body = "\n".join(short_line(d) for d in dolls) or "这里还空空的，去娃娃机试试手气吧～"
    embed = discord.Embed(title="🧸 我的收藏柜", description=f"{_m(user_id)} · 共 {total} 只\n\n{body}", color=COLOR_INFO)
    embed.set_footer(text=f"筛选：{FILTER_LABELS[flt]} · 第 {page + 1}/{pages} 页 · 🏆展示中 ✨彩蛋 🎁赠送中")
    return embed


def showcase_embed(user_id: int, dolls: list[dict], max_showcase: int) -> discord.Embed:
    if not dolls:
        return discord.Embed(title="🏆 娃娃展示柜", description=f"{_m(user_id)} 还没有摆出展示的娃娃。",
                             color=COLOR_INFO)
    embed = discord.Embed(title="🏆 娃娃展示柜", description=f"{_m(user_id)} 的展示位（{len(dolls)}/{max_showcase}）",
                          color=COLOR_EGG if any(d["egg"] for d in dolls) else COLOR_INFO)
    for d in dolls:
        value = f"异常特征：{d['names']['quirk']}"
        if d["egg"]:
            value += f"\n✨ {d['egg']['title']}"
        embed.add_field(name=f"🧸 {d['name']}（{d['id']}）", value=value, inline=False)
    return embed


def stats_embed(user_id: int, st: dict) -> discord.Embed:
    rate = f"（{st['wins'] / st['plays']:.0%}）" if st["plays"] else ""
    lines = [
        f"🎟️ 总抓取次数：{st['plays']}",
        f"🎉 成功抓取：{st['wins']}{rate}",
        f"🧸 当前持有：{st['holding']}（亲手抓的 {st['holding_caught']} · 别人送的 {st['holding_gifted']}）",
        f"🎲 历史独特组合：{st['unique_combos']}",
        f"✨ 亲自发现的彩蛋：{st['eggs']}/{st['eggs_total']}",
        f"🎁 已赠出：{st['gifts_sent']} · 已收到：{st['gifts_received']}",
    ]
    return discord.Embed(title="📊 娃娃机战绩", description=f"{_m(user_id)}\n\n" + "\n".join(lines), color=COLOR_INFO)


def gift_invite_embed(gift: dict, doll: dict) -> discord.Embed:
    lines = [f"{_m(gift['from_id'])} 想把一只娃娃送给 {_m(gift['to_id'])}！", ""] + doll_block(doll) + [
        "", f"编号：`{doll['id']}`", f"-# 邀请 <t:{_ts(gift['expires_at'])}:R> 过期；过期或拒绝后娃娃仍归原主人。"]
    return discord.Embed(title="🎁 南域游乐场 · 赠礼邀请", description="\n".join(lines),
                         color=COLOR_EGG if doll["egg"] else COLOR_GAME)


def gift_done_embed(gift: dict, doll: dict) -> discord.Embed:
    if gift["status"] == "accepted":
        lines = [f"{_m(gift['from_id'])} 赠送给 {_m(gift['to_id'])} 一只娃娃！", ""] + doll_block(doll) + [
            "", f"编号：`{doll['id']}`"]
        return discord.Embed(title="🎁 南域游乐场 · 神秘赠礼", description="\n".join(lines),
                             color=COLOR_EGG if doll["egg"] else COLOR_WIN)
    text = {"declined": "对方婉拒了这份赠礼", "cancelled": "赠送人撤回了这份赠礼",
            "expired": "这份赠礼邀请已过期"}.get(gift["status"], "赠礼已结束")
    return discord.Embed(title="🎁 赠礼邀请", color=COLOR_FAIL,
                         description=f"{text}，🧸 **{doll['name']}**（`{doll['id']}`）仍归 {_m(gift['from_id'])}。")

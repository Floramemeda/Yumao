"""超级滚雪球：面板、战报、终局与排行榜。

战报只读取 engine.push 返回的行动记录来描述，文案用独立 RNG 抽取，不会影响游戏结果。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import discord

from ..pet_spark.lines import pick
from . import engine
from .models import ACTION_EMOJI, ACTION_LABELS, TURN_DONE, TURN_SKIPPED, SnowballSession, Status

TITLE = "⛄ 超级滚雪球"
COLOR_LOBBY = discord.Color.from_rgb(170, 210, 255)
COLOR_GAME = discord.Color.from_rgb(120, 180, 255)
COLOR_REPORT = discord.Color.blurple()
COLOR_END = discord.Color.gold()
COLOR_CLOSED = discord.Color.dark_grey()

_PLACEHOLDERS = ("actor", "distance", "size", "item", "victim", "target")


def load_lines() -> dict:
    with open(Path(__file__).parent / "lines.json", encoding="utf-8") as f:
        return json.load(f)


def _m(uid: int) -> str:
    return f"<@{uid}>"


def _ts(dt) -> str:
    return f"<t:{int(dt.timestamp())}:R>"


def _line(lines: dict, group: str, rng: random.Random, **values) -> str:
    """只替换白名单占位符；值全部由程序生成（mention 或数字），不含用户输入。"""
    text = pick(lines, group, 0, rng)
    for key in _PLACEHOLDERS:
        if key in values:
            text = text.replace("{" + key + "}", str(values[key]))
    return text


def bar(value: int, maximum: int, full: str, empty: str) -> str:
    return full * value + empty * (maximum - value)


def size_text(level: int, cfg: dict) -> str:
    return f"Lv.{level} {bar(level, cfg['size_max'], '⚪', '·')}"


def momentum_text(momentum: int, cfg: dict) -> str:
    return f"{momentum} {bar(momentum, cfg['momentum_max'], '🔥', '▫️')}"


# ---------------------------------------------------------------- 面板

def lobby_embed(s: SnowballSession, cfg: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"{TITLE} · 招募中",
        description="大家轮流推同一颗雪球：越推越大、越滚越快，也越可能把下一个接棒的人卷进去！\n"
                    "全队共享一个总分，每个人都会把**完整的团队分**记进自己的历史积分。",
        color=COLOR_LOBBY,
    )
    embed.add_field(name="主持人", value=_m(s.creator_user_id), inline=True)
    embed.add_field(name="人数", value=f"{len(s.participants)} / {cfg['max_players']}", inline=True)
    if s.deadline:
        embed.add_field(name="报名截止", value=_ts(s.deadline), inline=True)
    embed.add_field(name="报名名单", value="、".join(_m(u) for u in s.participants) or "暂无", inline=False)
    embed.set_footer(text=f"{cfg['min_players']}–{cfg['max_players']} 人 · 每人两次推动机会")
    return embed


def _order_text(s: SnowballSession) -> str:
    marks = []
    for i, uid in enumerate(s.turn_queue):
        st = s.turn_status[i]
        if i == s.turn_index and s.status == Status.IN_PROGRESS:
            mark = "▶️"
        elif st == TURN_DONE:
            mark = "✅"
        elif st == TURN_SKIPPED:
            mark = "🌀"
        else:
            mark = "⏳"
        marks.append(f"{mark}{_m(uid)}")
    half = len(marks) // 2
    return "第一轮：" + " ".join(marks[:half]) + "\n第二轮：" + " ".join(marks[half:])


def panel_embed(s: SnowballSession, cfg: dict) -> discord.Embed:
    if s.status == Status.CANCELLED:
        return discord.Embed(title=f"{TITLE} · 已取消", description="本局已取消，不计分。", color=COLOR_CLOSED)
    finished = s.status in (Status.SETTLING, Status.COMPLETED)
    title = f"{TITLE} · 已结束" if finished else f"{TITLE} · 第 {s.action_sequence + 1} 棒"
    embed = discord.Embed(title=title, color=COLOR_END if finished else COLOR_GAME)
    if not finished and s.current_actor_id is not None:
        value = f"{_m(s.current_actor_id)}"
        if s.deadline:
            value += f"\n截止 {_ts(s.deadline)}"
        embed.add_field(name="当前接力", value=value, inline=True)
    embed.add_field(name="雪球", value=size_text(s.size_level, cfg), inline=True)
    embed.add_field(name="失控", value=momentum_text(s.momentum, cfg), inline=True)
    embed.add_field(name="距离", value=f"{s.distance_m} 米", inline=True)
    embed.add_field(name="杂物 / 撞击", value=f"{s.obstacle_count} 件 / {s.successful_hits} 次", inline=True)
    score = engine.score_breakdown(s, cfg)["total"]
    embed.add_field(name="团队得分" if finished else "暂计团队分（未结算）", value=str(score), inline=True)
    embed.add_field(name="已卷入", value="、".join(_m(u) for u in s.engulfed_user_ids) or "无人", inline=True)
    embed.add_field(name="剩余机会", value=str(s.remaining_turns()), inline=True)
    embed.add_field(name="接力顺序", value=_order_text(s), inline=False)
    if not finished:
        embed.set_footer(text="只有当前接力者能选择行动 · 超时将自动「稳稳地滚」")
    return embed


# ---------------------------------------------------------------- 战报

def report_embed(entry: dict, cfg: dict, lines: dict, flavor_seed: str) -> discord.Embed:
    rng = random.Random(flavor_seed)
    actor, action = _m(entry["actor"]), entry["action"]
    before, after = entry["before"], entry["after"]
    a = cfg["actions"][action]
    out = []
    if entry["auto"]:
        out.append(_line(lines, "auto", rng, actor=actor))
    out.append(_line(lines, f"action_{action}", rng, actor=actor, distance=a["distance"]))

    if after["size_level"] > before["size_level"]:
        group = "size_max" if after["size_level"] >= cfg["size_max"] else "size_up"
        out.append(_line(lines, group, rng, size=after["size_level"]))
    if entry["debris"]:
        out.append(_line(lines, "debris", rng, item=pick(lines, "debris_items", 0, rng)))
    if action == "aim":
        item = pick(lines, "hit_items", 0, rng)
        out.append(_line(lines, "hit" if entry["hit"] else "aim_miss", rng, item=item))

    cap = entry["capture"]
    if cap and cap["engulfed"]:
        out.append(_line(lines, "engulf", rng, victim=_m(cap["target"])))
        out.append(_line(lines, "voice", rng))
    elif cap and cap["probability"] >= 0.3:
        out.append(_line(lines, "near_miss", rng, target=_m(cap["target"])))

    stats = (f"-# {ACTION_EMOJI[action]} {ACTION_LABELS[action]} · 雪球 Lv.{after['size_level']} · "
             f"失控 {after['momentum']} · 共 {after['distance_m']} 米")
    if cap:
        stats += f" · 接棒卷入率 {cap['probability']:.0%}"
    out.append(stats)
    if entry["next_actor"] is not None:
        out.append(f"👉 下一棒：{_m(entry['next_actor'])}")
    return discord.Embed(title=f"第 {entry['seq'] + 1} 棒", description="\n".join(out), color=COLOR_REPORT)


# ---------------------------------------------------------------- 终局与排行榜

def final_embed(s: SnowballSession, cfg: dict, lines: dict, leaderboard: list[dict]) -> discord.Embed:
    rng = random.Random(f"{s.rng_seed}:final")
    kind = engine.end_kind(s)
    b = s.score_breakdown
    sc = cfg["score"]
    desc = [_line(lines, "final_all_engulfed" if kind == engine.END_ALL_ENGULFED else "final_normal", rng), ""]
    desc.append(f"## 🏆 团队总分 {b['total']}")
    desc.append(f"-# 距离 {s.distance_m} 米 = {b['distance']} · 雪球 Lv.{s.size_level} × {sc['per_size_level']} = {b['size']}"
                f" · 杂物 {s.obstacle_count} × {sc['per_debris']} = {b['debris']}"
                f" · 撞击 {s.successful_hits} × {sc['per_hit']} = {b['hits']}"
                f" · 卷入 {len(s.engulfed_user_ids)} × {sc['per_engulfed']} = {b['engulfed']}")
    embed = discord.Embed(title=f"{TITLE} · 接力完成！", description="\n".join(desc), color=COLOR_END)
    embed.add_field(name="被卷入", value="、".join(_m(u) for u in s.engulfed_user_ids) or "无人被卷入", inline=False)
    embed.add_field(name="本局入账", value="\n".join(f"{_m(u)} +{b['total']} 分" for u in s.participants), inline=True)
    if leaderboard:
        embed.add_field(name="本服累计积分榜", value=leaderboard_text(leaderboard[:5]), inline=True)
    return embed


def leaderboard_text(rows: list[dict]) -> str:
    medals = ["🥇", "🥈", "🥉"]
    return "\n".join(f"{medals[i] if i < 3 else f'{i + 1}.'} {_m(r['user_id'])} {r['total_score']} 分"
                     f"（{r['games_played']} 局）" for i, r in enumerate(rows))


def leaderboard_embed(rows: list[dict]) -> discord.Embed:
    embed = discord.Embed(title="⛄ 超级滚雪球 · 个人累计积分榜", color=COLOR_END)
    embed.description = leaderboard_text(rows) if rows else "还没有人完成过一局。"
    return embed

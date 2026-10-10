"""撸猫续火花：展示文案。签到回复保持简短，只有 /火花 用 Embed。"""
from __future__ import annotations

import random
from datetime import date

import discord

from . import lines as lines_mod
from .progress import SPARK_MIN_STREAK, CheckinResult, Level, Progress, current_streak, level_for, next_level

COLOR = discord.Color.from_rgb(255, 170, 90)


def level_text(lv: Level | None) -> str:
    return f"{lv.emoji} Lv.{lv.level} {lv.name}" if lv else "还不认识"


def streak_text(streak: int) -> str:
    if streak >= SPARK_MIN_STREAK:
        return f"🔥 火花 {streak} 天"
    return f"连续 {streak} 天"


def checkin_message(user_id: int, p: Progress, result: CheckinResult, today: date,
                    levels: list[Level], lines: dict, rng: random.Random) -> str:
    lv = level_for(p.total_checkin_days, levels)
    lv_no = lv.level if lv else 0
    streak = current_streak(p, today)

    if not result.counted:
        group = "repeat"
    elif result.spark_activated:
        group = "spark_activated"
    elif result.comeback:
        group = "comeback"
    elif result.first_ever:
        group = "first"
    else:
        group = "checkin"
    event = lines_mod.render(lines_mod.pick(lines, group, lv_no, rng),
                             streak=streak, level_name=lv.name if lv else "")
    meme = lines_mod.render(lines_mod.pick(lines, "meme", lv_no, rng),
                            streak=streak, level_name=lv.name if lv else "")

    head = "✅ 今日撸猫成功" if result.counted else "今天已经撸过啦"
    status = f"-# {head} · {streak_text(streak)} · {level_text(lv)}（累计 {p.total_checkin_days} 天）"
    parts = [f"<@{user_id}> {event}"]
    # 里程碑：只在当天首次有效签到、连续天数恰好命中时出现（重复撸 counted=False 不会触发）
    milestone = lines_mod.milestone_line(lines, streak, rng) if result.counted else None
    if milestone:
        parts.append("🏆 " + lines_mod.render(milestone, streak=streak, level_name=lv.name if lv else ""))
    parts += [meme, status]
    if result.level_up and lv:
        parts.append(f"-# 🎉 亲密等级提升：{level_text(lv)} —— {lv.description}")
    return "\n".join(parts)


def status_embed(p: Progress, today: date, levels: list[Level], reminders_on: bool) -> discord.Embed:
    streak = current_streak(p, today)
    checked = p.last_checkin_local_date == today
    lv = level_for(p.total_checkin_days, levels)
    nxt = next_level(p.total_checkin_days, levels)

    if streak >= SPARK_MIN_STREAK:
        spark = f"🔥 燃烧中（连续 {streak} 天）" if checked else f"⚠️ 今天还没撸！（连续 {streak} 天，午夜前撸猫才能保住）"
    elif streak == 1:
        spark = "✨ 再撸一天就能点亮" if checked else "✨ 今天撸猫就能点亮"
    else:
        spark = "💤 未点亮（连续撸猫 2 天点亮）"

    embed = discord.Embed(title="🐾 我的撸猫火花", color=COLOR)
    embed.add_field(name="今天", value="✅ 已撸猫" if checked else "❌ 还没撸猫", inline=True)
    embed.add_field(name="连续火花", value=spark, inline=True)
    embed.add_field(name="历史最高", value=f"{p.best_streak} 天", inline=True)
    embed.add_field(name="累计撸猫", value=f"{p.total_checkin_days} 天", inline=True)
    level_value = level_text(lv)
    if lv and lv.description:
        level_value += f"\n{lv.description}"
    if nxt:
        level_value += f"\n-# 距离 {nxt.emoji} {nxt.name} 还差 {nxt.threshold - p.total_checkin_days} 天"
    embed.add_field(name="亲密等级", value=level_value, inline=True)
    embed.add_field(name="22:00 催撸", value="🔔 开启" if reminders_on else "🔕 已关闭", inline=True)
    if streak == 0 and p.total_checkin_days > 0 and lv:
        embed.set_footer(text="火花熄灭了，但本喵记得你。")
    return embed

"""沙滩排球：面板、每球战报与终局。面板只显示谁选过，不显示选了什么；方向在战报里才公开。"""
from __future__ import annotations

import discord

from . import engine
from .models import (DIRECTION_LABELS, END_IDLE, END_WIPEOUT, GARMENT_LABELS, PART_LABELS, PHASE_SERVE, Status,
                     TEAM_NAMES, VolleyballSession)

TITLE = "🏐 沙滩排球"
COLOR_LOBBY = discord.Color.from_rgb(255, 214, 140)
COLOR_GAME = discord.Color.from_rgb(80, 190, 220)
COLOR_REPORT = discord.Color.blurple()
COLOR_END = discord.Color.gold()
COLOR_CLOSED = discord.Color.dark_grey()


def _m(uid: int) -> str:
    return f"<@{uid}>"


def _ts(dt) -> str:
    return f"<t:{int(dt.timestamp())}:R>"


def rules_text(cfg: dict) -> str:
    parts = cfg["parts"]
    return (f"开局随机分两队。发球人偷偷选 左边 / 右边，对方全队各自选部位和方向，一起接！\n"
            f"方向猜中才碰得到球，碰到后看能不能接住：\n"
            f"• 手：稳（约 {parts['hand']['chance']:.0%}），接住 +{parts['hand']['score']}\n"
            f"• 胸 / 屁股：险（约 {parts['chest']['chance']:.0%}），接住 +{parts['chest']['score']}，"
            f"但碰到球就会弄坏上衣 / 下衣\n"
            f"服饰坏了还用那个部位，发情度 25 → 50 → 100，到 100 失能退场。\n"
            f"猜错方向或没接住，有 {cfg['friendly_fire']['chance']:.0%} 概率把球砸到队友的胸或屁股上！\n"
            f"全队都没接住，发球方 +{cfg['miss_score']}。")


def lobby_embed(s: VolleyballSession, cfg: dict) -> discord.Embed:
    embed = discord.Embed(title=f"{TITLE} · 招募中", description=rules_text(cfg), color=COLOR_LOBBY)
    embed.add_field(name="主持人", value=_m(s.creator_user_id), inline=True)
    embed.add_field(name="目标分", value=str(s.target_score), inline=True)
    embed.add_field(name=f"已报名（{len(s.participants)}/{cfg['max_players']}，至少 {cfg['min_players']} 人）",
                    value="\n".join(_m(u) for u in s.participants) or "还没有人", inline=False)
    if s.deadline:
        embed.add_field(name="报名截止", value=_ts(s.deadline), inline=False)
    return embed


def player_line(s: VolleyballSession, uid: int) -> str:
    p = s.players[str(uid)]
    if p["out"]:
        return f"💤 {_m(uid)} 失能退场"
    wear = ("👕" if p["top"] else "💥") + ("🩳" if p["bottom"] else "💥")
    mark = ""
    if s.status == Status.IN_PROGRESS:
        if s.phase == PHASE_SERVE and uid == s.server_id:
            mark = " 🏐"
        elif s.phase != PHASE_SERVE and str(uid) in s.choices:
            mark = " ✅"
    return f"{wear} {_m(uid)} 发情 {p['excitement']}{mark}"


def _add_teams(embed: discord.Embed, s: VolleyballSession) -> None:
    for t in (0, 1):
        embed.add_field(name=f"{TEAM_NAMES[t]} · {s.scores[t]} 分",
                        value="\n".join(player_line(s, u) for u in s.teams[t]), inline=True)


def panel_embed(s: VolleyballSession, cfg: dict) -> discord.Embed:
    if s.status == Status.CANCELLED:
        return discord.Embed(title=f"{TITLE} · 已取消", color=COLOR_CLOSED)
    if s.status == Status.COMPLETED:
        embed = discord.Embed(title=f"{TITLE} · 已结束", color=COLOR_CLOSED)
        _add_teams(embed, s)
        return embed
    if s.phase == PHASE_SERVE:
        text = f"第 {s.round_no + 1} 球：请 {_m(s.server_id)} 选择发球方向（别人看不到）。"
    else:
        team = TEAM_NAMES[1 - s.serving_team]
        done, total = len(s.choices), len(engine.receivers(s))
        text = (f"第 {s.round_no + 1} 球：{_m(s.server_id)} 发球了！\n"
                f"{team} 每人先点方向、再选部位接球，选了不能改（{done}/{total}）。")
    if s.deadline:
        text += f"\n截止：{_ts(s.deadline)}"
    embed = discord.Embed(title=f"{TITLE} · 目标 {s.target_score} 分", description=text, color=COLOR_GAME)
    _add_teams(embed, s)
    return embed


def result_line(r: dict) -> str:
    who = _m(r["user"])
    if r["result"] == "idle":
        return f"💨 {who} 发呆，没接"
    how = f"用{PART_LABELS[r['part']]}接{DIRECTION_LABELS[r['guess']]}"
    if r["result"] == "wrong":
        return f"🙈 {who} {how}，方向猜错了"
    text = f"✅ {who} {how}，接住了！+{r['score']}" if r["result"] == "caught" else f"❌ {who} {how}，碰到了但没接住"
    if "broke" in r:
        text += f" · {GARMENT_LABELS[r['broke']]}坏了！"
    if "excitement" in r:
        text += f" · 发情度 {r['excitement'][0]} → {r['excitement'][1]}"
    if r.get("out"):
        text += " · 💤 失能退场！"
    if "friendly" in r:
        text += "\n" + friendly_line(r["friendly"])
    return text


def friendly_line(hit: dict) -> str:
    text = f"　↳ 💢 球砸到了队友 {_m(hit['target'])} 的{PART_LABELS[hit['part']]}上"
    if "broke" in hit:
        text += f" · {GARMENT_LABELS[hit['broke']]}坏了！"
    if "excitement" in hit:
        text += f" · 发情度 {hit['excitement'][0]} → {hit['excitement'][1]}"
    if hit.get("out"):
        text += " · 💤 失能退场！"
    return text


def report_embed(entry: dict) -> discord.Embed:
    auto = "（超时自动发球）" if entry["auto"] else ""
    lines = [f"{_m(entry['server'])} 发向 **{DIRECTION_LABELS[entry['direction']]}** {auto}".rstrip(), ""]
    lines += [result_line(r) for r in entry["results"]]
    if entry.get("miss_score"):
        lines += ["", f"全队没接住，{TEAM_NAMES[entry['serving_team']]} +{entry['miss_score']}"]
    else:
        lines += ["", f"{TEAM_NAMES[1 - entry['serving_team']]} +{entry['gained']}"]
    embed = discord.Embed(title=f"🏐 第 {entry['round']} 球", description="\n".join(lines), color=COLOR_REPORT)
    embed.set_footer(text=f"{TEAM_NAMES[0]} {entry['scores'][0]} : {entry['scores'][1]} {TEAM_NAMES[1]}")
    return embed


def final_embed(s: VolleyballSession) -> discord.Embed:
    if s.end_reason == END_IDLE:
        embed = discord.Embed(title=f"{TITLE} · 比赛中止", description="连续几球都没有人操作，大家都回家了。",
                              color=COLOR_CLOSED)
    else:
        how = "对方全员失能！" if s.end_reason == END_WIPEOUT else f"率先拿到 {s.target_score} 分！"
        embed = discord.Embed(title=f"{TITLE} · {TEAM_NAMES[s.winner]} 获胜", description=how, color=COLOR_END)
    _add_teams(embed, s)
    best = max(s.participants, key=lambda u: s.players[str(u)]["points"])
    if s.players[str(best)]["points"] > 0:
        embed.add_field(name="全场最佳", value=f"{_m(best)} · 贡献 {s.players[str(best)]['points']} 分", inline=False)
    return embed

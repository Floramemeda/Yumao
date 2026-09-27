"""千杯不醉：Embed 与公告文案。"""
from __future__ import annotations

import random

import discord

from . import engine
from .models import ACTION_LABELS, Action, DrinkingGame, GameStatus, PlayerState, PlayerStatus

TITLE = "🍻 千杯不醉"
COLOR_LOBBY = discord.Color.gold()
COLOR_ACTIVE = discord.Color.orange()
COLOR_SETTLE = discord.Color.blurple()
COLOR_END = discord.Color.dark_grey()
COLOR_WIN = discord.Color.green()

DESC_LIMIT = 4000
ORDINALS = ["第一杯", "第二杯", "第三杯", "第四杯", "第五杯"]


def _mention(user_id: int) -> str:
    return f"<@{user_id}>"


def _ts(dt, style: str = "R") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def _signed(delta: int) -> str:
    return f"+{delta}%" if delta >= 0 else f"{delta}%"


def progress_bar(value: int, maximum: int = 100, width: int = 10) -> str:
    filled = round(value / maximum * width)
    return "▰" * filled + "▱" * (width - filled)


def _join_limited(lines: list[str], sep: str = "\n") -> str:
    """拼接行，超出 Embed 描述长度时截断并注明剩余数量。"""
    out, length = [], 0
    for i, line in enumerate(lines):
        if length + len(line) + len(sep) > DESC_LIMIT - 30:
            out.append(f"……以及其他 {len(lines) - i} 项")
            break
        out.append(line)
        length += len(line) + len(sep)
    return sep.join(out)


def lobby_embed(game: DrinkingGame) -> discord.Embed:
    players = list(game.players.values())
    embed = discord.Embed(
        title=f"{TITLE} · 招募中",
        description="酒已温好，谁来一决高下？\n醉酒度达到 100% 即淘汰，最后一个清醒的人获胜！",
        color=COLOR_LOBBY,
    )
    embed.add_field(name="房主", value=_mention(game.host_id), inline=True)
    embed.add_field(name="当前人数", value=str(len(players)), inline=True)
    roster = "、".join(_mention(p.user_id) for p in players) or "暂无"
    if len(roster) > 1000:
        roster = roster[:1000].rsplit("、", 1)[0] + " ……"
    embed.add_field(name="参与者", value=roster, inline=False)
    embed.set_footer(text="至少 2 人才能开始 · 只有房主可以开始比赛")
    return embed


def _player_line(game: DrinkingGame, p: PlayerState) -> str:
    bar = progress_bar(p.drunkenness)
    if p.status == PlayerStatus.ACTIVE:
        if game.status == GameStatus.ACTIVE:
            state = "已行动" if p.action_for(game.round_number) else "等待中"
        else:
            state = "清醒"
    elif p.status == PlayerStatus.WITHDRAWN:
        state = "已退出"
    elif p.status == PlayerStatus.ELIMINATED:
        state = "已淘汰"
    else:
        state = "🏆 获胜"
    return f"{_mention(p.user_id)} `{bar}` {p.drunkenness}% / {state}"


def panel_embed(game: DrinkingGame) -> discord.Embed:
    """比赛开始后的常驻公共面板。"""
    if game.status == GameStatus.CANCELLED:
        return discord.Embed(title=f"{TITLE} · 已取消", description="本场酒局已取消。", color=COLOR_END)

    lines = [_player_line(game, p) for p in game.players.values()]
    if game.status == GameStatus.FINISHED:
        embed = discord.Embed(title=f"{TITLE} · 比赛结束", color=COLOR_END,
                              description=_join_limited(lines))
        embed.set_footer(text=f"共进行 {game.round_number} 轮")
        return embed

    embed = discord.Embed(title=f"{TITLE} · 第 {game.round_number} 轮", color=COLOR_ACTIVE,
                          description=_join_limited(lines))
    if game.status == GameStatus.ACTIVE and game.round_deadline:
        embed.add_field(name="行动截止", value=_ts(game.round_deadline), inline=True)
        embed.add_field(name="行动进度", value=f"{game.submitted_count()} / {len(game.active_players())}",
                        inline=True)
    else:
        embed.add_field(name="状态", value="结算中……", inline=True)
    embed.set_footer(text="🍺 再来一杯 · 😏 假装喝了 · 🏳️ 放弃比赛 | 提交后不可修改，超时视为放弃")
    return embed


def _result_block(result: dict) -> str:
    uid = result["user_id"]
    action = Action(result["action"])
    lines = [f"**{_mention(uid)}**"]

    if action == Action.WITHDRAW:
        lines.append(result["flavor"] or "放弃了比赛。")
        lines.append(f"退出前醉酒度：{result['after']}%。")
        return "\n".join(lines)

    draws = result["draws"]
    if action == Action.DRINK:
        d = draws[0]
        verb = "抽到了" if d["kind"] == "alcohol" else "吃到了"
        lines.append(f"{verb}{d['name']}，醉酒度 {_signed(d['delta'])}。")
    elif result["pretend_success"]:
        lines.append(result["flavor"])
    else:
        lines.append(result["flavor"])
        for i, d in enumerate(draws):
            lines.append(f"- {ORDINALS[i] if i < len(ORDINALS) else f'第{i + 1}杯'}：{d['name']} {_signed(d['delta'])}")
        if len(draws) < 3 and result["eliminated"]:
            lines.append("- 还没喝完就倒下了……")
        lines.append(f"本轮醉酒度 {_signed(result['after'] - result['before'])}。")

    if result["eliminated"]:
        if action == Action.DRINK and result["flavor"]:
            lines.append(result["flavor"])
        lines.append(f"当前醉酒度：{result['after']}%，**已淘汰**。")
    else:
        lines.append(f"当前醉酒度：{result['after']}%。")
    return "\n".join(lines)


def settlement_embeds(result: dict) -> list[discord.Embed]:
    """回合结算 Embed；玩家过多时拆分为多个 Embed。"""
    blocks = [_result_block(r) for r in result["players"]]
    embeds: list[discord.Embed] = []
    current: list[str] = []
    length = 0
    for block in blocks:
        if current and length + len(block) + 2 > DESC_LIMIT:
            embeds.append(discord.Embed(description="\n\n".join(current), color=COLOR_SETTLE))
            current, length = [], 0
        current.append(block)
        length += len(block) + 2
    if current or not embeds:
        embeds.append(discord.Embed(description="\n\n".join(current) or "本轮无人行动。", color=COLOR_SETTLE))
    embeds[0].title = f"第 {result['round']} 轮 · 结算"
    return embeds


def announcement(game: DrinkingGame, result: dict, events: dict, rng: random.Random) -> tuple[str, discord.Embed]:
    """最终公告：返回 (消息内容, Embed)。消息内容 mention 获胜者以触发提醒。"""
    outcome = result["outcome"]
    winners = [game.players[uid] for uid in outcome["winner_ids"]]
    total = len(game.players)

    if outcome["kind"] == engine.OUTCOME_ALL_DOWN:
        embed = discord.Embed(title=f"{TITLE} · 无人生还", description=events["all_down"], color=COLOR_END)
        embed.add_field(name="坚持轮数", value=str(game.round_number), inline=True)
        embed.add_field(name="参赛人数", value=str(total), inline=True)
        return "", embed

    mentions = " ".join(_mention(p.user_id) for p in winners)
    if outcome["kind"] == engine.OUTCOME_WINNER:
        desc = f"恭喜 {mentions}！\n\n{rng.choice(events['victory'])}"
    elif len(winners) == 1:
        desc = f"恭喜 {mentions}！\n\n众人纷纷离席，而你离席时最清醒！"
    else:
        desc = f"恭喜 {mentions} 并列获胜！\n\n众人纷纷离席，而你们离席时最清醒！"

    embed = discord.Embed(title="🏆 千杯不醉 · 酒王诞生！", description=desc, color=COLOR_WIN)
    final = "、".join(f"{p.drunkenness}%" for p in winners) if len(winners) > 1 else f"{winners[0].drunkenness}%"
    embed.add_field(name="最终醉酒度", value=final, inline=True)
    embed.add_field(name="坚持轮数", value=str(game.round_number), inline=True)
    embed.add_field(name="参赛人数", value=str(total), inline=True)
    return mentions, embed


def action_confirm(action: Action) -> str:
    return f"本轮行动已提交：{ACTION_LABELS[action]}。"

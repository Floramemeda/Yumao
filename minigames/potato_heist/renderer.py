"""媚笙阁偷薯饼：Embed 与文案。

公共面板与播报只接受 engine.public_view / 回合结果作为输入，从结构上避免泄露鼠的秘密。
"""
from __future__ import annotations

import discord

from . import engine
from .models import NODE_NAMES, Role, Status, describe, parse

TITLE = "🥔 媚笙阁偷薯饼"
COLOR_LOBBY = discord.Color.gold()
COLOR_GAME = discord.Color.orange()
COLOR_ROUND = discord.Color.blurple()
COLOR_END = discord.Color.dark_grey()
COLOR_CAT = discord.Color.red()
COLOR_MICE = discord.Color.green()

RULES = (
    "**角色**：1 名猫猫警长，其余都是鼠鼠。\n"
    "**地图**：一楼「大堂」「后厨」；二楼「二楼走廊」和四个雅间「枕风阁 / 隔花阁 / 幔雪阁 / 帘月阁」。\n"
    "**每轮**：所有人在截止前通过「我的行动」秘密选择，可反复修改，截止时以最后一次为准，然后同时结算。\n\n"
    "🐱 **警长**（从大堂出发，只站在大堂或二楼走廊）\n"
    "· 每轮必须：上楼 / 下楼 / 搜查本楼层一个房间（一楼查后厨，二楼查雅间）\n"
    "· 房间被查后，接下来两轮不能再查（搜空也算）\n"
    "· 上下楼巡逻后与鼠同在大堂或二楼走廊 → 抓住；搜查时鼠在被查房内 → 抓住（查房那轮不在公共区抓人）\n\n"
    "🐭 **鼠鼠**（随机出生在某个雅间）\n"
    "· 每轮沿通路移动一格，或在房间（雅间/后厨）内等待；在公共区不能等待\n"
    "· 某个雅间与后厨之间有一条只有鼠知道的**密道**，走一次花一轮\n"
    "· 在后厨花一轮「偷薯饼」，带着薯饼离开后厨后**再活过一整轮**，鼠方获胜\n"
    "· 携带者被抓，薯饼回到后厨，其他鼠还能再偷\n\n"
    "**胜负**：任意一鼠成功 → 鼠方胜；全部鼠被抓，或 10 轮结束仍未成功 → 警长胜。\n"
    "超时未选：鼠原地不动，警长由系统代选（优先搜查）。"
)


def _m(uid: int) -> str:
    return f"<@{uid}>"


def _ts(dt) -> str:
    return f"<t:{int(dt.timestamp())}:R>"


def cooldown_text(searched_round: int) -> str:
    return f"第 {searched_round + 1}、{searched_round + 2} 轮冷却"


def rules_embed() -> discord.Embed:
    return discord.Embed(title=f"{TITLE} · 规则", description=RULES, color=COLOR_LOBBY)


def lobby_embed(pub: dict, cfg: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"{TITLE} · 招募中",
        description="媚笙阁后厨新出炉了一盘薯饼……猫猫警长今夜巡楼，鼠鼠们能把它偷出去吗？",
        color=COLOR_LOBBY,
    )
    embed.add_field(name="主持人", value=_m(pub["host_id"]), inline=True)
    embed.add_field(name="人数", value=f"{len(pub['roster'])} / {cfg['max_players']}", inline=True)
    if pub["deadline"]:
        embed.add_field(name="报名截止", value=_ts(pub["deadline"]), inline=True)
    embed.add_field(name="报名名单", value="、".join(_m(u) for u in pub["roster"]) or "暂无", inline=False)
    embed.set_footer(text=f"{cfg['min_players']}–{cfg['max_players']} 人 · 开局随机抽 1 人当猫猫警长")
    return embed


def _search_lines(pub: dict) -> list[str]:
    lines = []
    for entry in pub["search_log"][-6:]:
        room = NODE_NAMES[entry["room"]]
        result = "抓到 " + "、".join(_m(u) for u in entry["captured"]) if entry["captured"] else "一无所获"
        cd = " · " + cooldown_text(entry["round"]) if pub["cooldowns"].get(entry["room"]) == entry["round"] + 3 else ""
        lines.append(f"第 {entry['round']} 轮 · {room}：{result}{cd}")
    return lines


def panel_embed(pub: dict, cfg: dict) -> discord.Embed:
    status = Status(pub["status"])
    if status in (Status.CANCELLED, Status.EXPIRED):
        text = "本局已取消。" if status == Status.CANCELLED else "报名超时，本局已关闭。"
        return discord.Embed(title=f"{TITLE} · 已关闭", description=text, color=COLOR_END)

    if status == Status.FINISHED:
        side = "🐱 警长获胜" if pub["winner"] == Role.CAT.value else "🐭 鼠鼠获胜"
        title, color = f"{TITLE} · 游戏结束（{side}）", COLOR_END
    else:
        title, color = f"{TITLE} · 第 {pub['round_no']} / {cfg['max_rounds']} 轮", COLOR_GAME

    embed = discord.Embed(title=title, color=color)
    floor = "一楼（大堂）" if pub["cat_floor"] == 1 else "二楼（二楼走廊）"
    embed.add_field(name="猫猫警长", value=f"{_m(pub['cat_id'])}\n当前在{floor}", inline=True)
    embed.add_field(name="存活鼠鼠", value=f"{pub['alive_mice']} / {pub['mice_total']}", inline=True)
    embed.add_field(name="薯饼", value="⚠️ 已失窃" if pub["potato_stolen"] else "🥔 还在后厨", inline=True)
    if status == Status.ROUND_INPUT and pub["deadline"]:
        embed.add_field(name="行动截止", value=_ts(pub["deadline"]), inline=True)
        embed.add_field(name="已选择", value=f"{pub['submitted']} / {pub['actors']}", inline=True)
    embed.add_field(name="查房记录", value="\n".join(_search_lines(pub)) or "暂无", inline=False)
    embed.add_field(name="已被捕", value="、".join(_m(u) for u in pub["captured_ids"]) or "暂无", inline=False)
    if status == Status.ROUND_INPUT:
        embed.set_footer(text="点「我的行动」查看身份、位置并秘密选择行动 · 截止前可修改")
    return embed


def round_embed(result: dict) -> discord.Embed:
    """回合播报：只含警长动作、抓捕和薯饼事件，不含鼠的位置与动作。"""
    lines = []
    kind, arg = parse(result["cat_action"])
    auto = "（超时，系统代选）" if result["cat_auto"] else ""
    if kind == "search":
        lines.append(f"🐱 警长搜查了**{NODE_NAMES[arg]}**{auto}。")
    else:
        lines.append(f"🐱 警长{describe(result['cat_action'])}{auto}。")

    public_caught = [c["user_id"] for c in result["captures"] if c["how"] == "public"]
    if public_caught:
        where = NODE_NAMES[result["cat_position"]]
        lines.append(f"🚨 警长在{where}逮住了 " + "、".join(_m(u) for u in public_caught) + "！")
    if result["search"] is not None:
        caught = result["search"]["captured"]
        if caught:
            lines.append("🚨 搜查抓到了 " + "、".join(_m(u) for u in caught) + "！")
        else:
            lines.append("🔍 搜查一无所获。")
        lines.append(f"（{NODE_NAMES[result['search']['room']]}：{cooldown_text(result['round'])}）")

    if result["potato_event"] == "stolen":
        lines.append("🥔 **薯饼失窃！** 后厨的薯饼不见了！")
    elif result["potato_event"] == "recovered":
        lines.append("🥔 **薯饼追回！** 薯饼被送回了后厨。")

    return discord.Embed(title=f"第 {result['round']} 轮 · 结算", description="\n".join(lines), color=COLOR_ROUND)


def final_embed(pub: dict, result: dict) -> tuple[str, discord.Embed]:
    reason = result["win_reason"]
    rounds = result["round"]
    if result["winner"] == Role.CAT.value:
        mention = _m(pub["cat_id"])
        text = ("所有鼠鼠都被抓住了！" if reason == engine.CAT_WIN_ALL_CAPTURED
                else f"{rounds} 轮过去，薯饼没能离开媚笙阁！")
        embed = discord.Embed(title="🐱 警长获胜！", description=f"恭喜 {mention}！{text}", color=COLOR_CAT)
    else:
        winners = [u for u in pub["roster"] if u != pub["cat_id"]]
        mention = " ".join(_m(u) for u in winners)
        embed = discord.Embed(title="🐭 鼠鼠获胜！",
                              description=f"恭喜 {mention}！薯饼被成功运出了媚笙阁！", color=COLOR_MICE)
    embed.add_field(name="进行轮数", value=str(rounds), inline=True)
    embed.add_field(name="被捕鼠鼠", value=f"{len(pub['captured_ids'])} / {pub['mice_total']}", inline=True)
    return mention, embed


def private_embed(view: dict, round_no: int, max_rounds: int, notice: str | None = None) -> discord.Embed:
    pos = NODE_NAMES[view["position"]]
    if view["role"] == Role.CAT.value:
        embed = discord.Embed(title="🐱 你是猫猫警长", color=COLOR_CAT)
        embed.add_field(name="位置", value=f"{pos}（{view['floor']} 楼）", inline=True)
        rooms = "、".join(NODE_NAMES[r] for r in view["searchable"]) or "本楼层暂无可查房间"
        embed.add_field(name="本楼层可搜查", value=rooms, inline=True)
    else:
        embed = discord.Embed(title="🐭 你是鼠鼠", color=COLOR_MICE)
        embed.add_field(name="位置", value=pos if not view["captured"] else f"{pos}（已被捕）", inline=True)
        embed.add_field(name="密道", value=f"{NODE_NAMES[view['secret_room']]} ⇄ 后厨（只有鼠知道）", inline=True)
        if view["carrying"]:
            if view["escaped_at"] is None:
                status = "🥔 你拿着薯饼！离开后厨（走大堂或密道），再活过一整轮就赢了。"
            else:
                status = "🥔 你拿着薯饼并已离开后厨，活过本轮就赢了！"
            embed.add_field(name="薯饼", value=status, inline=False)

    if view["captured"]:
        embed.description = "你已经被抓住了，只能看大家继续了。"
    else:
        chosen = view["current_action"]
        embed.description = (f"第 {round_no} / {max_rounds} 轮 · 本轮选择："
                             f"**{describe(chosen) if chosen else '尚未选择'}**\n截止前可以改选。")
    if view["note"]:
        embed.add_field(name="提示", value=view["note"], inline=False)
    if notice:
        embed.add_field(name="​", value=notice, inline=False)
    return embed

"""媚笙阁偷薯饼：纯规则逻辑，不依赖 Discord。

随机性只出现在开局分配（猫、密道、鼠出生点）；回合结算完全确定，
同样的状态与行动永远得到同样的结果。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .models import (
    CORRIDOR, DOWN, FLOOR, HALL, KITCHEN, MOUSE_EDGES, NODE_NAMES, ROOMS, SEARCHABLE, STAY, STEAL, SUITES, UP,
    WAIT, HeistGame, Player, Role, describe, move, parse, search,
)

_DIR = Path(__file__).parent

CAT_WIN_ALL_CAPTURED = "all_captured"
CAT_WIN_TIME_UP = "time_up"
MICE_WIN_ESCAPED = "escaped"


class ActionError(Exception):
    """提交了非法动作；message 可直接展示给玩家。"""


def load_config() -> dict:
    with open(_DIR / "config.json", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 开局

def assign_roles(game: HeistGame, rng: random.Random) -> None:
    """随机选 1 猫、随机密道雅间，鼠随机出生在四个雅间之一（允许同房）。"""
    ids = list(game.players)
    cat_id = rng.choice(ids)
    game.secret_room = rng.choice(SUITES)
    for uid in ids:
        p = game.players[uid]
        if uid == cat_id:
            p.role, p.position = Role.CAT, HALL
        else:
            p.role, p.position = Role.MOUSE, rng.choice(SUITES)


# ---------------------------------------------------------------- 合法动作

def mouse_neighbors(game: HeistGame, node: str) -> list[str]:
    out = list(MOUSE_EDGES[node])
    if node == game.secret_room:
        out.append(KITCHEN)
    elif node == KITCHEN and game.secret_room:
        out.append(game.secret_room)
    return out


def legal_actions(game: HeistGame, user_id: int) -> list[str]:
    """服务端权威的合法动作列表；猫的列表永远非空（上/下楼总可用）。"""
    p = game.players.get(user_id)
    if p is None or p.role is None:
        return []
    if p.role == Role.CAT:
        actions = [UP] if p.position == HALL else [DOWN]
        actions += [search(r) for r in SEARCHABLE[FLOOR[p.position]] if game.searchable(r)]
        return actions
    if p.captured:
        return []
    actions = [move(n) for n in mouse_neighbors(game, p.position)]
    if p.position in ROOMS:
        actions.append(WAIT)
    if p.position == KITCHEN and game.potato_carrier_id is None:
        actions.append(STEAL)
    return actions


def default_action(game: HeistGame, user_id: int) -> str:
    """超时默认行动：鼠原地不动；猫优先查本楼层第一个可查房间，否则上下楼。"""
    p = game.players[user_id]
    if p.role == Role.MOUSE:
        return STAY
    for room in SEARCHABLE[FLOOR[p.position]]:
        if game.searchable(room):
            return search(room)
    return UP if p.position == HALL else DOWN


def submit_action(game: HeistGame, user_id: int, action: str) -> None:
    """记录本轮选择（可覆盖）。所有校验都基于服务端状态。"""
    p = game.players.get(user_id)
    if p is None:
        raise ActionError("你没有参加本局游戏")
    if p.role == Role.MOUSE and p.captured:
        raise ActionError("你已经被抓住了，无法继续行动")
    if action not in legal_actions(game, user_id):
        raise ActionError("这个动作现在不可用")
    p.action = action
    p.action_round = game.round_no


# ---------------------------------------------------------------- 回合结算

def resolve_round(game: HeistGame, max_rounds: int) -> dict:
    """按固定顺序结算当前回合并判定胜负。会修改 game，返回可公开播报的结果。"""
    r = game.round_no

    # 1. 封存行动；未提交或已失效的选择按默认行动处理
    sealed: dict[int, str] = {}
    cat_auto = False
    for p in game.actors():
        chosen = p.action_for(r)
        if chosen is None or chosen not in legal_actions(game, p.user_id):
            chosen = default_action(game, p.user_id)
            p.auto_note = f"第 {r} 轮你没有及时行动，系统代选：{describe(chosen)}"
            p.auto_note_round = r
            if p.role == Role.CAT:
                cat_auto = True
        sealed[p.user_id] = chosen

    carrier_at_start = game.potato_carrier_id
    carrier_was_in_kitchen = carrier_at_start is not None and game.players[carrier_at_start].position == KITCHEN

    # 2. 同时更新位置（SEARCH / STEAL / WAIT / STAY 不移动）
    for uid, action in sealed.items():
        kind, arg = parse(action)
        p = game.players[uid]
        if kind == "up":
            p.position = CORRIDOR
        elif kind == "down":
            p.position = HALL
        elif kind == "move":
            p.position = arg

    cat = game.cat
    captures: list[dict] = []

    def capture(p: Player, how: str) -> None:
        p.captured, p.captured_round = True, r
        captures.append({"user_id": p.user_id, "how": how, "node": p.position})

    # 3. 公共区域同位置抓捕（只看结算后的位置，不判路径交叉）
    for m in game.alive_mice():
        if m.position == cat.position:
            capture(m, "public")

    # 4. 搜查：按鼠行动后的最终位置抓捕，并设置两轮冷却（搜空同样生效）
    search_info = None
    kind, room = parse(sealed[cat.user_id])
    if kind == "search":
        caught = [m for m in game.alive_mice() if m.position == room]
        for m in caught:
            capture(m, "search")
        game.search_next_available_round[room] = r + 3
        search_info = {"round": r, "room": room, "captured": [m.user_id for m in caught]}
        game.search_log.append(search_info)

    # 携带者被捕：薯饼回到后厨，本轮不能被其他鼠顺手偷走
    potato_event = None
    if carrier_at_start is not None and game.players[carrier_at_start].captured:
        game.potato_carrier_id = None
        game.escaped_kitchen_at_round = None
        potato_event = "recovered"

    # 5. 偷窃：仅当本轮开始时薯饼无人携带；多鼠同时偷按入局顺序取第一个
    if carrier_at_start is None:
        for m in game.alive_mice():
            if m.position == KITCHEN and sealed.get(m.user_id) == STEAL:
                game.potato_carrier_id = m.user_id
                game.escaped_kitchen_at_round = None
                potato_event = "stolen"
                break

    # 6. 逃脱进度：本轮开始已携带、开始时在后厨、本轮最终离开后厨
    if (carrier_at_start is not None and game.potato_carrier_id == carrier_at_start
            and carrier_was_in_kitchen and game.players[carrier_at_start].position != KITCHEN
            and game.escaped_kitchen_at_round is None):
        game.escaped_kitchen_at_round = r

    # 7. 胜负：捕获先于偷窃与获胜判定
    if not game.alive_mice():
        game.winner, game.win_reason = Role.CAT, CAT_WIN_ALL_CAPTURED
    elif (game.potato_carrier_id is not None and game.escaped_kitchen_at_round is not None
          and r > game.escaped_kitchen_at_round):
        game.winner, game.win_reason = Role.MOUSE, MICE_WIN_ESCAPED
    elif r >= max_rounds:
        game.winner, game.win_reason = Role.CAT, CAT_WIN_TIME_UP

    return {
        "round": r,
        "cat_action": sealed[cat.user_id],
        "cat_auto": cat_auto,
        "cat_position": cat.position,
        "captures": captures,
        "search": search_info,
        "potato_event": potato_event,
        "winner": game.winner.value if game.winner else None,
        "win_reason": game.win_reason,
    }


# ---------------------------------------------------------------- 视图

def public_view(game: HeistGame) -> dict:
    """公开信息。这里绝不包含鼠位置、鼠动作、密道和携带者。"""
    cat = game.cat
    cooldowns = {room: nxt for room, nxt in game.search_next_available_round.items() if nxt > game.round_no}
    return {
        "status": game.status.value,
        "host_id": game.host_id,
        "round_no": game.round_no,
        "deadline": game.deadline,
        "roster": list(game.players),
        "cat_id": cat.user_id if cat else None,
        "cat_floor": FLOOR[cat.position] if cat else None,
        "mice_total": len(game.mice()),
        "alive_mice": len(game.alive_mice()),
        "captured_ids": [m.user_id for m in game.mice() if m.captured],
        "search_log": list(game.search_log),
        "cooldowns": cooldowns,
        "potato_stolen": game.potato_carrier_id is not None,
        "submitted": game.submitted_count(),
        "actors": len(game.actors()),
        "winner": game.winner.value if game.winner else None,
        "win_reason": game.win_reason,
    }


def private_view(game: HeistGame, user_id: int) -> dict | None:
    p = game.players.get(user_id)
    if p is None or p.role is None:
        return None
    view = {
        "role": p.role.value,
        "position": p.position,
        "floor": FLOOR[p.position],
        "captured": p.captured,
        "legal_actions": legal_actions(game, user_id),
        "current_action": p.action_for(game.round_no),
        "note": p.auto_note if p.auto_note_round == game.round_no - 1 else None,
    }
    if p.role == Role.MOUSE:
        carrying = game.potato_carrier_id == user_id
        view.update({
            "secret_room": game.secret_room,
            "carrying": carrying,
            "escaped_at": game.escaped_kitchen_at_round if carrying else None,
        })
    else:
        view["searchable"] = [r for r in SEARCHABLE[FLOOR[p.position]] if game.searchable(r)]
    return view


def node_name(node: str) -> str:
    return NODE_NAMES[node]

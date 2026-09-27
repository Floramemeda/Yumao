"""千杯不醉：纯游戏逻辑（RNG、醉酒度、胜负判定），不依赖 Discord。

结算结果使用可 JSON 序列化的 dict，便于持久化后在重启时补发，而不重跑 RNG。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .models import Action, DrinkingGame, PlayerState, PlayerStatus

_DIR = Path(__file__).parent

OUTCOME_CONTINUE = "continue"
OUTCOME_WINNER = "winner"          # 仅剩一名清醒玩家
OUTCOME_TIEBREAK = "tiebreak"      # 无人清醒，按本轮退出者的退出前醉酒度判定
OUTCOME_ALL_DOWN = "all_down"      # 全员醉倒，无人获胜


def load_config() -> dict:
    with open(_DIR / "config.json", encoding="utf-8") as f:
        return json.load(f)


def load_events() -> dict:
    with open(_DIR / "events.json", encoding="utf-8") as f:
        return json.load(f)


def clamp(value: int, cfg: dict) -> int:
    return max(0, min(cfg["max_drunkenness"], value))


def draw_once(rng: random.Random, cfg: dict, events: dict) -> dict:
    """「再来一杯」的一次随机抽取：90% 酒类 +5~15，10% 解酒食物 -5。"""
    drink = cfg["drink"]
    if rng.random() < drink["alcohol_probability"]:
        low, high = drink["alcohol_increase"]
        return {"kind": "alcohol", "name": rng.choice(events["alcohols"]), "delta": rng.randint(low, high)}
    return {"kind": "food", "name": rng.choice(events["foods"]), "delta": -drink["food_decrease"]}


def drink_sequence(start: int, count: int, rng: random.Random, cfg: dict, events: dict) -> tuple[list[dict], int]:
    """连续执行 count 次普通饮酒抽取；醉酒度到顶立即停止后续抽取。"""
    draws = []
    current = start
    for _ in range(count):
        draw = draw_once(rng, cfg, events)
        current = clamp(current + draw["delta"], cfg)
        draws.append({**draw, "after": current})
        if current >= cfg["max_drunkenness"]:
            break
    return draws, current


def resolve_player(player: PlayerState, action: Action, timed_out: bool,
                   rng: random.Random, cfg: dict, events: dict) -> dict:
    """计算单个玩家本轮结果（不修改玩家状态）。"""
    result = {
        "user_id": player.user_id,
        "action": action.value,
        "timed_out": timed_out,
        "pretend_success": None,
        "draws": [],
        "before": player.drunkenness,
        "after": player.drunkenness,
        "eliminated": False,
        "flavor": None,
    }
    if action == Action.WITHDRAW:
        result["flavor"] = rng.choice(events["timeout" if timed_out else "withdraw"])
        return result

    if action == Action.PRETEND:
        success = rng.random() < cfg["pretend"]["success_probability"]
        result["pretend_success"] = success
        result["flavor"] = rng.choice(events["pretend_success" if success else "pretend_caught"])
        if success:
            return result
        count = cfg["pretend"]["failure_drink_count"]
    else:
        count = 1

    # 作弊失败复用普通饮酒抽取
    draws, after = drink_sequence(player.drunkenness, count, rng, cfg, events)
    result["draws"] = draws
    result["after"] = after
    if after >= cfg["max_drunkenness"]:
        result["eliminated"] = True
        result["flavor"] = rng.choice(events["eliminated"]) if action == Action.DRINK else result["flavor"]
    return result


def determine_outcome(game: DrinkingGame) -> dict:
    """按优先级判定胜负（需在本轮状态更新后调用）。"""
    active = game.active_players()
    if len(active) >= 2:
        return {"kind": OUTCOME_CONTINUE, "winner_ids": []}
    if len(active) == 1:
        return {"kind": OUTCOME_WINNER, "winner_ids": [active[0].user_id]}

    # 只比较本轮退出的玩家；本轮醉倒和历史退出的玩家不参与
    withdrawn_now = [
        p for p in game.players.values()
        if p.status == PlayerStatus.WITHDRAWN and p.withdrawn_round == game.round_number
    ]
    if withdrawn_now:
        lowest = min(p.drunkenness for p in withdrawn_now)
        return {"kind": OUTCOME_TIEBREAK, "winner_ids": [p.user_id for p in withdrawn_now if p.drunkenness == lowest]}
    return {"kind": OUTCOME_ALL_DOWN, "winner_ids": []}


def settle_round(game: DrinkingGame, rng: random.Random, cfg: dict, events: dict) -> dict:
    """结算当前回合：补超时退出、执行 RNG、更新玩家状态并判定胜负。

    按玩家加入顺序遍历，结果与提交先后无关。会修改 game 中的玩家状态。
    """
    round_number = game.round_number
    results = []
    for player in game.active_players():
        action = player.action_for(round_number)
        timed_out = action is None
        if timed_out:
            action = Action.WITHDRAW
        results.append(resolve_player(player, action, timed_out, rng, cfg, events))

    for result in results:
        player = game.players[result["user_id"]]
        player.drunkenness = result["after"]
        if result["action"] == Action.WITHDRAW.value:
            player.status = PlayerStatus.WITHDRAWN
            player.withdrawn_round = round_number
        elif result["eliminated"]:
            player.status = PlayerStatus.ELIMINATED

    outcome = determine_outcome(game)
    for user_id in outcome["winner_ids"]:
        game.players[user_id].status = PlayerStatus.WINNER

    return {"round": round_number, "players": results, "outcome": outcome}

"""超级滚雪球：纯规则逻辑（不依赖 Discord）。

可重放：每次推动的随机数来自 Random(f"{rng_seed}:{action_sequence}")，
只要种子和已确认的行动序列相同，结果就完全一致；无需持久化 RNG 内部状态。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .models import ACTIONS, TURN_DONE, TURN_PENDING, TURN_SKIPPED, SnowballSession

_DIR = Path(__file__).parent

END_NORMAL = "normal"
END_ALL_ENGULFED = "all_engulfed"   # 「全员滚蛋」：基础规则下不可达，仅保留判定与模板


def load_config() -> dict:
    with open(_DIR / "config.json", encoding="utf-8") as f:
        return json.load(f)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- 开局

def start(session: SnowballSession, seed: str, cfg: dict) -> None:
    """冻结名单、随机接力顺序、生成两轮机会队列、初始化雪球。"""
    session.rng_seed = seed
    order = list(session.participants)
    random.Random(f"{seed}:order").shuffle(order)
    session.relay_order = order
    session.turn_queue = order * cfg["turns_per_player"]
    session.turn_status = [TURN_PENDING] * len(session.turn_queue)
    session.turn_index = 0
    session.action_sequence = 0
    session.size_level = cfg["size_min"]
    session.momentum = cfg["momentum_min"]
    session.distance_m = session.obstacle_count = session.successful_hits = 0
    session.engulfed_user_ids = []
    session.action_log = []


# ---------------------------------------------------------------- 判定

def capture_probability(size_level: int, momentum: int, action: str, cfg: dict) -> float:
    """接棒碾压率（百分点相加后 clamp 到 [0, max]）。size/momentum 为本棒更新后的值。"""
    cap = cfg["capture"]
    a = cfg["actions"][action]
    p = cap["base_by_size"][size_level - 1] + momentum * cap["per_momentum"] + a["capture_mod"] + a["protection"]
    return round(clamp(p, 0.0, cap["max"]), 6)


def _weighted(rng: random.Random, options: list[list]) -> int:
    values = [o[0] for o in options]
    weights = [o[1] for o in options]
    return rng.choices(values, weights=weights)[0]


def next_pending_index(session: SnowballSession, after: int) -> int | None:
    for i in range(after + 1, len(session.turn_queue)):
        if session.turn_status[i] == TURN_PENDING:
            return i
    return None


def capture_target(session: SnowballSession, actor: int) -> int | None:
    """下一个尚未卷入、仍有推动机会、且不是当前推动者的玩家。"""
    for i in range(session.turn_index + 1, len(session.turn_queue)):
        uid = session.turn_queue[i]
        if session.turn_status[i] == TURN_PENDING and uid != actor and uid not in session.engulfed_user_ids:
            return uid
    return None


def push(session: SnowballSession, action: str, cfg: dict, auto: bool = False) -> dict:
    """执行当前棒的推动并推进队列。返回完整行动记录（输入、随机结果、前后状态）。"""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action}")
    actor = session.current_actor_id
    if actor is None:
        raise RuntimeError("no current actor")

    seq = session.action_sequence
    rng = random.Random(f"{session.rng_seed}:{seq}")
    a = cfg["actions"][action]
    before = snapshot(session)

    # 1. 行动增量 → 2. clamp
    size_delta = _weighted(rng, a["size_delta"])
    session.distance_m += a["distance"]
    session.size_level = clamp(session.size_level + size_delta, cfg["size_min"], cfg["size_max"])
    session.momentum = clamp(session.momentum + a["momentum"], cfg["momentum_min"], cfg["momentum_max"])

    # 3. 障碍：普通杂物与有效撞击分开计数
    debris = 1 if a["debris"] and rng.random() < cfg["debris_chance"] else 0
    hit = 1 if a["hit_chance"] > 0 and rng.random() < a["hit_chance"] else 0
    session.obstacle_count += debris
    session.successful_hits += hit

    # 当前机会已消费
    session.turn_status[session.turn_index] = TURN_DONE

    # 4. 接棒卷入：每棒最多一次，对象是下一位等待接棒者
    capture = None
    target = capture_target(session, actor)
    if target is not None:
        p = capture_probability(session.size_level, session.momentum, action, cfg)
        roll = rng.random()
        engulfed = roll < p
        capture = {"target": target, "probability": p, "roll": round(roll, 6), "engulfed": engulfed}
        if engulfed:
            session.engulfed_user_ids.append(target)
            for i, uid in enumerate(session.turn_queue):
                if uid == target and session.turn_status[i] == TURN_PENDING:
                    session.turn_status[i] = TURN_SKIPPED

    nxt = next_pending_index(session, session.turn_index)
    session.action_sequence += 1
    session.turn_index = nxt if nxt is not None else len(session.turn_queue)

    entry = {
        "seq": seq,
        "actor": actor,
        "action": action,
        "auto": auto,
        "size_delta": size_delta,
        "debris": debris,
        "hit": hit,
        "capture": capture,
        "before": before,
        "after": snapshot(session),
        "next_actor": session.turn_queue[nxt] if nxt is not None else None,
        "finished": nxt is None,
    }
    session.action_log.append(entry)
    return entry


def snapshot(session: SnowballSession) -> dict:
    return {
        "size_level": session.size_level,
        "momentum": session.momentum,
        "distance_m": session.distance_m,
        "obstacle_count": session.obstacle_count,
        "successful_hits": session.successful_hits,
        "engulfed": len(session.engulfed_user_ids),
    }


# ---------------------------------------------------------------- 计分

def score_breakdown(session: SnowballSession, cfg: dict) -> dict:
    s = cfg["score"]
    parts = {
        "distance": session.distance_m * s["per_meter"],
        "size": session.size_level * s["per_size_level"],
        "debris": session.obstacle_count * s["per_debris"],
        "hits": session.successful_hits * s["per_hit"],
        "engulfed": len(set(session.engulfed_user_ids)) * s["per_engulfed"],
    }
    parts["total"] = sum(parts.values())
    return parts


def end_kind(session: SnowballSession) -> str:
    if session.participants and set(session.participants) <= set(session.engulfed_user_ids):
        return END_ALL_ENGULFED
    return END_NORMAL


def replay(participants: list[int], seed: str, actions: list[str], cfg: dict) -> SnowballSession:
    """用同一种子和行动序列重放一局，用于调查和测试。"""
    from datetime import datetime, timezone
    from .models import Status
    s = SnowballSession("replay", 0, 0, 0, participants[0], Status.IN_PROGRESS, datetime.now(timezone.utc),
                        participants=list(participants))
    start(s, seed, cfg)
    for act in actions:
        if s.current_actor_id is None:
            break
        push(s, act, cfg)
    return s

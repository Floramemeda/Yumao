"""超级滚雪球 Monte Carlo 平衡性模拟（可复现）。

python -m minigames.snowball_relay.simulate            默认每组 10000 局
python -m minigames.snowball_relay.simulate --games 2000 --seed 7
"""
from __future__ import annotations

import argparse
import random
import statistics
from collections import Counter

from . import engine
from .models import ACTIONS


def _expected_gain(session, action, cfg) -> float:
    """贪心策略用：本棒期望即时得分（距离 + 期望体积分 + 期望杂物/撞击分），不考虑卷人风险。"""
    a = cfg["actions"][action]
    s = cfg["score"]
    exp_size = sum(min(cfg["size_max"], session.size_level + d) * w for d, w in a["size_delta"]) / sum(
        w for _, w in a["size_delta"])
    gain = a["distance"] * s["per_meter"] + (exp_size - session.size_level) * s["per_size_level"]
    if a["debris"]:
        gain += cfg["debris_chance"] * s["per_debris"]
    gain += a["hit_chance"] * s["per_hit"]
    return gain


def _greedy_risk(session, action, cfg) -> float:
    """风险感知贪心：即时期望分 − 卷人后失去对方剩余机会的期望损失（粗估每次机会 ~150 分）。"""
    gain = _expected_gain(session, action, cfg)
    a = cfg["actions"][action]
    size = min(cfg["size_max"], session.size_level + max(d for d, _ in a["size_delta"]))
    mom = engine.clamp(session.momentum + a["momentum"], cfg["momentum_min"], cfg["momentum_max"])
    p = engine.capture_probability(size, mom, action, cfg)
    target = engine.capture_target(session, session.current_actor_id)
    if target is None:
        return gain
    lost = sum(1 for i, u in enumerate(session.turn_queue)
               if u == target and session.turn_status[i] == "pending")
    return gain - p * (lost * 150 - cfg["score"]["per_engulfed"])


STRATEGIES = {
    "纯稳推": lambda s, cfg, rng: "steady",
    "纯猛推": lambda s, cfg, rng: "hard",
    "纯瞄准": lambda s, cfg, rng: "aim",
    "纯修整": lambda s, cfg, rng: "repair",
    "随机": lambda s, cfg, rng: rng.choice(ACTIONS),
    "自适应": lambda s, cfg, rng: "hard" if s.size_level <= 3 else ("aim" if s.momentum <= 1 else "repair"),
    "即时贪心": lambda s, cfg, rng: max(ACTIONS, key=lambda a: _expected_gain(s, a, cfg)),
    "风险贪心": lambda s, cfg, rng: max(ACTIONS, key=lambda a: _greedy_risk(s, a, cfg)),
}


def play(n_players: int, strategy, cfg: dict, seed: str, rng: random.Random):
    from datetime import datetime, timezone
    from .models import SnowballSession, Status
    s = SnowballSession("sim", 0, 0, 0, 1, Status.IN_PROGRESS, datetime.now(timezone.utc),
                        participants=list(range(1, n_players + 1)))
    engine.start(s, seed, cfg)
    used = Counter()
    while s.current_actor_id is not None:
        act = strategy(s, cfg, rng)
        used[act] += 1
        engine.push(s, act, cfg)
    return engine.score_breakdown(s, cfg)["total"], s.action_sequence, len(s.engulfed_user_ids), used


def run(games: int, seed: int, cfg: dict | None = None) -> list[dict]:
    cfg = cfg or engine.load_config()
    rows = []
    for n in range(cfg["min_players"], cfg["max_players"] + 1):
        for name, strat in STRATEGIES.items():
            rng = random.Random(f"{seed}:{n}:{name}")
            scores, turns, engulfed, used = [], [], [], Counter()
            for g in range(games):
                sc, t, e, u = play(n, strat, cfg, f"{seed}:{n}:{name}:{g}", rng)
                scores.append(sc)
                turns.append(t)
                engulfed.append(e)
                used.update(u)
            scores.sort()
            total_used = sum(used.values())
            rows.append({
                "players": n, "strategy": name,
                "mean": statistics.fmean(scores), "median": statistics.median(scores),
                "p95": scores[int(0.95 * (len(scores) - 1))],
                "turns": statistics.fmean(turns), "engulfed": statistics.fmean(engulfed),
                "usage": {a: used[a] / total_used for a in ACTIONS},
            })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()
    rows = run(args.games, args.seed)
    print(f"{'人数':<4}{'策略':<8}{'均值':>8}{'中位':>8}{'P95':>8}{'棒数':>7}{'卷入':>6}  动作使用率")
    for r in rows:
        usage = " ".join(f"{a}:{v:.0%}" for a, v in r["usage"].items() if v)
        print(f"{r['players']:<6}{r['strategy']:<8}{r['mean']:>8.0f}{r['median']:>8.0f}{r['p95']:>8.0f}"
              f"{r['turns']:>7.2f}{r['engulfed']:>6.2f}  {usage}")


if __name__ == "__main__":
    main()

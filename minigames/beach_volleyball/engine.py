"""沙滩排球：纯规则逻辑（不依赖 Discord）。

一球分两步：发球人选方向（保密）→ 对方全队各自选部位和方向。方向猜中才碰得到球，碰到后按部位的成功率判定接没接住。
胸 / 屁股碰到球就会弄坏对应服饰（耐久 1）；服饰坏了再碰，发情度按 25 → 50 → 100 递进，到顶失能退场。
猜错方向或没接住的人有概率误伤一个队友（打到胸或屁股，效果同上）。
接住的人给本队加部位分；全队都没接住，发球方 +1。随机数来自 Random(f"{rng_seed}:{seq}:…")，同种子可重放。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .models import END_IDLE, END_TARGET, END_WIPEOUT, PHASE_RECEIVE, PHASE_SERVE, VolleyballSession

_DIR = Path(__file__).parent


def load_config() -> dict:
    with open(_DIR / "config.json", encoding="utf-8") as f:
        return json.load(f)


def new_player() -> dict:
    return {"top": True, "bottom": True, "excitement": 0, "out": False, "points": 0}


def start(s: VolleyballSession, seed: str) -> None:
    """冻结名单、随机分两队（人数差最多 1）、随机一队的一个人先发球。"""
    s.rng_seed = seed
    rng = random.Random(f"{seed}:start")
    order = list(s.participants)
    rng.shuffle(order)
    half = len(order) // 2
    s.teams = [order[:half], order[half:]]
    s.scores = [0, 0]
    s.players = {str(u): new_player() for u in order}
    s.serving_team = rng.randrange(2)
    s.server_id = rng.choice(s.teams[s.serving_team])
    s.phase, s.seq, s.round_no, s.idle_rounds = PHASE_SERVE, 0, 0, 0
    s.direction, s.serve_auto, s.choices = None, False, {}


def active(s: VolleyballSession, team: int) -> list[int]:
    return [u for u in s.teams[team] if not s.players[str(u)]["out"]]


def receivers(s: VolleyballSession) -> list[int]:
    return active(s, 1 - s.serving_team)


def serve(s: VolleyballSession, direction: str, auto: bool = False) -> None:
    s.direction, s.serve_auto, s.choices = direction, auto, {}
    s.phase = PHASE_RECEIVE
    s.seq += 1


def auto_direction(s: VolleyballSession, directions: list[str]) -> str:
    return random.Random(f"{s.rng_seed}:{s.seq}:auto").choice(directions)


def all_chosen(s: VolleyballSession) -> bool:
    return all(str(u) in s.choices for u in receivers(s))


def _wear(player: dict, garment: str | None, cfg: dict, result: dict) -> None:
    """球碰到身体的后果：先坏服饰，服饰没了才涨发情度。"""
    if garment is None:
        return
    if player[garment]:
        player[garment] = False
        result["broke"] = garment
        return
    steps = cfg["excitement_steps"]
    before = player["excitement"]
    player["excitement"] = next(x for x in steps if x > before)
    result["excitement"] = [before, player["excitement"]]
    if player["excitement"] >= steps[-1]:
        player["out"] = True
        result["out"] = True


def resolve(s: VolleyballSession, cfg: dict) -> dict:
    """结算本球，返回战报记录。会修改 s。"""
    seq = s.seq
    team = 1 - s.serving_team
    results, catchers, gained = [], [], 0
    for uid in receivers(s):
        choice = s.choices.get(str(uid))
        if choice is None:
            results.append({"user": uid, "result": "idle"})
            continue
        part, guess = choice
        r = {"user": uid, "part": part, "guess": guess}
        results.append(r)
        if guess != s.direction:
            r["result"] = "wrong"
            continue
        spec = cfg["parts"][part]
        player = s.players[str(uid)]
        _wear(player, spec.get("garment"), cfg, r)
        if random.Random(f"{s.rng_seed}:{seq}:{uid}").random() < spec["chance"]:
            r["result"] = "caught"
            r["score"] = spec["score"]
            player["points"] += spec["score"]
            gained += spec["score"]
            catchers.append(uid)
        else:
            r["result"] = "dropped"

    _friendly_fire(s, results, team, cfg, seq)

    entry = {"round": s.round_no + 1, "server": s.server_id, "direction": s.direction, "auto": s.serve_auto,
             "serving_team": s.serving_team, "results": results, "gained": gained, "next_server": None}
    idle = s.serve_auto and not s.choices
    s.idle_rounds = s.idle_rounds + 1 if idle else 0
    if catchers:
        s.scores[team] += gained
    else:
        s.scores[s.serving_team] += cfg["miss_score"]
        entry["miss_score"] = cfg["miss_score"]
    entry["scores"] = list(s.scores)
    s.round_no += 1

    winner = next((t for t in (0, 1) if s.scores[t] >= s.target_score), None)
    if winner is not None:
        s.winner, s.end_reason = winner, END_TARGET
    elif not active(s, team):
        s.winner, s.end_reason = s.serving_team, END_WIPEOUT
    elif s.idle_rounds >= cfg["max_idle_rounds"]:
        s.end_reason = END_IDLE
    entry["finished"] = s.end_reason is not None

    if not entry["finished"]:
        _next_serve(s, team, catchers, seq)
        entry["next_server"] = s.server_id
    s.seq += 1
    s.direction, s.serve_auto, s.choices = None, False, {}
    s.phase = PHASE_SERVE
    return entry


def _friendly_fire(s: VolleyballSession, results: list[dict], team: int, cfg: dict, seq: int) -> None:
    """猜错方向或没接住的人，有一定概率把球打到同队另一个还在场的人的胸或屁股上（效果同自己碰到）。

    在所有人自己的结果都定了之后再算，所以被误伤失能不影响他本球已经接住的分。"""
    ff = cfg["friendly_fire"]
    for r in results:
        if r["result"] not in ("wrong", "dropped"):
            continue
        rng = random.Random(f"{s.rng_seed}:{seq}:{r['user']}:ff")
        mates = [u for u in active(s, team) if u != r["user"]]
        if not mates or rng.random() >= ff["chance"]:
            continue
        hit = {"target": rng.choice(mates), "part": rng.choice(ff["parts"])}
        _wear(s.players[str(hit["target"])], cfg["parts"][hit["part"]].get("garment"), cfg, hit)
        r["friendly"] = hit


def _next_serve(s: VolleyballSession, team: int, catchers: list[int], seq: int) -> None:
    """接住了：球打回去，由还在场的接球人之一发；没接住：发球方队内轮到下一个人。"""
    if catchers:
        pool = [u for u in catchers if not s.players[str(u)]["out"]] or active(s, team)
        s.serving_team = team
        s.server_id = random.Random(f"{s.rng_seed}:{seq}:next").choice(pool)
        return
    roster = s.teams[s.serving_team]
    i = roster.index(s.server_id)
    s.server_id = next(u for u in roster[i + 1:] + roster[:i + 1] if not s.players[str(u)]["out"])

"""沙滩排球：会话状态。全部可序列化为 JSON，重启后据此恢复。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

DIRECTIONS = ["left", "right"]
DIRECTION_LABELS = {"left": "左边", "right": "右边"}
PARTS = ["hand", "chest", "butt"]
PART_LABELS = {"hand": "手", "chest": "胸", "butt": "屁股"}
GARMENT_LABELS = {"top": "上衣", "bottom": "下衣"}
TEAM_NAMES = ["🌊 海浪队", "☀️ 烈日队"]

PHASE_SERVE = "serve"
PHASE_RECEIVE = "receive"

END_TARGET = "target"       # 先到目标分
END_WIPEOUT = "wipeout"     # 一队全员失能
END_IDLE = "idle"           # 连续几球没人操作，中止


class Status(str, Enum):
    LOBBY = "LOBBY"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


TERMINAL = {Status.COMPLETED, Status.CANCELLED}


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


@dataclass
class VolleyballSession:
    id: str
    guild_id: int
    channel_id: int
    message_id: int
    creator_user_id: int
    status: Status
    created_at: datetime
    target_score: int

    participants: list[int] = field(default_factory=list)   # 报名期按加入顺序，开局时冻结
    teams: list[list[int]] = field(default_factory=list)    # 两队，开局随机分
    scores: list[int] = field(default_factory=lambda: [0, 0])
    players: dict[str, dict] = field(default_factory=dict)  # uid -> {top, bottom, excitement, out, points}

    phase: str = PHASE_SERVE
    seq: int = 0                    # 每次换阶段 +1：旧按钮和旧计时器据此失效
    round_no: int = 0               # 已结算的球数
    server_id: int | None = None
    serving_team: int = 0
    direction: str | None = None    # 本球方向，结算前不公开
    serve_auto: bool = False        # 本球是超时自动发的
    choices: dict[str, list[str]] = field(default_factory=dict)   # uid -> [部位, 方向]
    idle_rounds: int = 0

    rng_seed: str = ""
    deadline: datetime | None = None
    pending_reports: list[dict] = field(default_factory=list)     # 已提交未播报的战报（outbox）
    winner: int | None = None
    end_reason: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "guild_id": self.guild_id, "channel_id": self.channel_id,
            "message_id": self.message_id, "creator_user_id": self.creator_user_id,
            "status": self.status.value, "created_at": _iso(self.created_at), "target_score": self.target_score,
            "participants": self.participants, "teams": self.teams, "scores": self.scores, "players": self.players,
            "phase": self.phase, "seq": self.seq, "round_no": self.round_no, "server_id": self.server_id,
            "serving_team": self.serving_team, "direction": self.direction, "serve_auto": self.serve_auto,
            "choices": self.choices, "idle_rounds": self.idle_rounds, "rng_seed": self.rng_seed,
            "deadline": _iso(self.deadline), "pending_reports": self.pending_reports, "winner": self.winner,
            "end_reason": self.end_reason, "started_at": _iso(self.started_at), "ended_at": _iso(self.ended_at),
        }

    @classmethod
    def from_dict(cls, d: dict) -> VolleyballSession:
        return cls(
            id=d["id"], guild_id=int(d["guild_id"]), channel_id=int(d["channel_id"]),
            message_id=int(d["message_id"]), creator_user_id=int(d["creator_user_id"]),
            status=Status(d["status"]), created_at=_dt(d["created_at"]), target_score=int(d["target_score"]),
            participants=[int(x) for x in d.get("participants", [])],
            teams=[[int(x) for x in t] for t in d.get("teams", [])],
            scores=[int(x) for x in d.get("scores", [0, 0])], players=d.get("players", {}),
            phase=d.get("phase", PHASE_SERVE), seq=int(d.get("seq", 0)), round_no=int(d.get("round_no", 0)),
            server_id=int(d["server_id"]) if d.get("server_id") is not None else None,
            serving_team=int(d.get("serving_team", 0)), direction=d.get("direction"),
            serve_auto=bool(d.get("serve_auto", False)), choices=d.get("choices", {}),
            idle_rounds=int(d.get("idle_rounds", 0)), rng_seed=d.get("rng_seed", ""),
            deadline=_dt(d.get("deadline")), pending_reports=d.get("pending_reports", []), winner=d.get("winner"),
            end_reason=d.get("end_reason"), started_at=_dt(d.get("started_at")), ended_at=_dt(d.get("ended_at")),
        )

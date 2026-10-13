"""超级滚雪球：会话状态。全部可序列化为 JSON，重启后据此恢复。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

ACTIONS = ["steady", "hard", "aim", "repair"]
ACTION_LABELS = {
    "steady": "稳稳地滚",
    "hard": "用力猛推",
    "aim": "瞄准撞击",
    "repair": "修整雪球",
}
ACTION_EMOJI = {"steady": "⛄", "hard": "💪", "aim": "🎯", "repair": "🧤"}

TURN_PENDING = "pending"
TURN_DONE = "done"
TURN_SKIPPED = "skipped"   # 机会所属玩家已被卷入


class Status(str, Enum):
    LOBBY = "LOBBY"
    IN_PROGRESS = "IN_PROGRESS"
    SETTLING = "SETTLING"      # 终局分已算出、正在记账/播报
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


TERMINAL = {Status.COMPLETED, Status.CANCELLED}


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


@dataclass
class SnowballSession:
    id: str
    guild_id: int
    channel_id: int
    message_id: int
    creator_user_id: int
    status: Status
    created_at: datetime

    participants: list[int] = field(default_factory=list)   # 开局时冻结；报名期按加入顺序
    relay_order: list[int] = field(default_factory=list)    # 随机排列
    turn_queue: list[int] = field(default_factory=list)     # 两轮机会（玩家 id）
    turn_status: list[str] = field(default_factory=list)    # 每个机会的状态
    turn_index: int = 0                                     # 当前机会下标
    action_sequence: int = 0                                # 已完成的推动次数

    size_level: int = 1
    momentum: int = 0
    distance_m: int = 0
    obstacle_count: int = 0
    successful_hits: int = 0
    engulfed_user_ids: list[int] = field(default_factory=list)  # 有序、无重复

    action_log: list[dict] = field(default_factory=list)
    rng_seed: str = ""

    deadline: datetime | None = None          # 报名截止或本棒截止
    pending_reports: list[dict] = field(default_factory=list)  # 已提交未播报的战报（outbox）
    final_team_score: int | None = None
    score_breakdown: dict | None = None
    awards: dict | None = None                # 结算时每人实际入账（含幂等跳过信息）

    started_at: datetime | None = None
    ended_at: datetime | None = None
    settled_at: datetime | None = None
    version: int = 0

    @property
    def current_actor_id(self) -> int | None:
        if self.status != Status.IN_PROGRESS or self.turn_index >= len(self.turn_queue):
            return None
        return self.turn_queue[self.turn_index]

    def remaining_turns(self) -> int:
        return sum(1 for s in self.turn_status if s == TURN_PENDING)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "guild_id": self.guild_id, "channel_id": self.channel_id,
            "message_id": self.message_id, "creator_user_id": self.creator_user_id,
            "status": self.status.value, "created_at": _iso(self.created_at),
            "participants": self.participants, "relay_order": self.relay_order,
            "turn_queue": self.turn_queue, "turn_status": self.turn_status,
            "turn_index": self.turn_index, "action_sequence": self.action_sequence,
            "size_level": self.size_level, "momentum": self.momentum, "distance_m": self.distance_m,
            "obstacle_count": self.obstacle_count, "successful_hits": self.successful_hits,
            "engulfed_user_ids": self.engulfed_user_ids, "action_log": self.action_log,
            "rng_seed": self.rng_seed, "deadline": _iso(self.deadline),
            "pending_reports": self.pending_reports, "final_team_score": self.final_team_score,
            "score_breakdown": self.score_breakdown, "awards": self.awards,
            "started_at": _iso(self.started_at), "ended_at": _iso(self.ended_at),
            "settled_at": _iso(self.settled_at), "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SnowballSession:
        return cls(
            id=d["id"], guild_id=int(d["guild_id"]), channel_id=int(d["channel_id"]),
            message_id=int(d["message_id"]), creator_user_id=int(d["creator_user_id"]),
            status=Status(d["status"]), created_at=_dt(d["created_at"]),
            participants=[int(x) for x in d.get("participants", [])],
            relay_order=[int(x) for x in d.get("relay_order", [])],
            turn_queue=[int(x) for x in d.get("turn_queue", [])],
            turn_status=list(d.get("turn_status", [])),
            turn_index=int(d.get("turn_index", 0)), action_sequence=int(d.get("action_sequence", 0)),
            size_level=int(d.get("size_level", 1)), momentum=int(d.get("momentum", 0)),
            distance_m=int(d.get("distance_m", 0)), obstacle_count=int(d.get("obstacle_count", 0)),
            successful_hits=int(d.get("successful_hits", 0)),
            engulfed_user_ids=[int(x) for x in d.get("engulfed_user_ids", [])],
            action_log=d.get("action_log", []), rng_seed=d.get("rng_seed", ""),
            deadline=_dt(d.get("deadline")), pending_reports=d.get("pending_reports", []),
            final_team_score=d.get("final_team_score"), score_breakdown=d.get("score_breakdown"),
            awards=d.get("awards"),
            started_at=_dt(d.get("started_at")), ended_at=_dt(d.get("ended_at")),
            settled_at=_dt(d.get("settled_at")), version=int(d.get("version", 0)),
        )

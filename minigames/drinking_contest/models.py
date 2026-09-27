"""千杯不醉：游戏与玩家状态。所有状态都可以序列化为 JSON 以便重启恢复。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class GameStatus(str, Enum):
    LOBBY = "LOBBY"
    ACTIVE = "ACTIVE"
    SETTLING = "SETTLING"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"


class PlayerStatus(str, Enum):
    ACTIVE = "ACTIVE"
    WITHDRAWN = "WITHDRAWN"
    ELIMINATED = "ELIMINATED"
    WINNER = "WINNER"


class Action(str, Enum):
    DRINK = "drink"
    PRETEND = "pretend"
    WITHDRAW = "withdraw"


ACTION_LABELS = {
    Action.DRINK: "再来一杯",
    Action.PRETEND: "假装喝了",
    Action.WITHDRAW: "放弃比赛",
}


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class PlayerState:
    user_id: int
    display_name: str

    drunkenness: int = 0
    status: PlayerStatus = PlayerStatus.ACTIVE

    # 行动按轮次隔离：只有 submitted_round == 当前轮次时 submitted_action 才有效
    submitted_action: Action | None = None
    submitted_round: int = 0

    # 退出发生的轮次（用于"本轮退出"的特殊胜负判定）
    withdrawn_round: int | None = None

    def action_for(self, round_number: int) -> Action | None:
        if self.submitted_round == round_number:
            return self.submitted_action
        return None

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "drunkenness": self.drunkenness,
            "status": self.status.value,
            "submitted_action": self.submitted_action.value if self.submitted_action else None,
            "submitted_round": self.submitted_round,
            "withdrawn_round": self.withdrawn_round,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PlayerState:
        action = data.get("submitted_action")
        return cls(
            user_id=int(data["user_id"]),
            display_name=data["display_name"],
            drunkenness=int(data["drunkenness"]),
            status=PlayerStatus(data["status"]),
            submitted_action=Action(action) if action else None,
            submitted_round=int(data.get("submitted_round", 0)),
            withdrawn_round=data.get("withdrawn_round"),
        )


@dataclass
class DrinkingGame:
    game_id: str

    guild_id: int
    channel_id: int
    message_id: int

    host_id: int

    status: GameStatus
    round_number: int

    created_at: datetime

    # dict 保持加入顺序；结算按加入顺序遍历，与提交顺序无关
    players: dict[int, PlayerState] = field(default_factory=dict)

    round_started_at: datetime | None = None
    round_deadline: datetime | None = None
    # 已完成结算（RNG 已执行）的最大轮次，防止重复结算
    settled_round: int = 0
    # 已结算但尚未发布的回合结果（重启后据此补发，不重跑 RNG）
    pending_result: dict | None = None

    started_at: datetime | None = None
    finished_at: datetime | None = None

    def active_players(self) -> list[PlayerState]:
        return [p for p in self.players.values() if p.status == PlayerStatus.ACTIVE]

    def eligible_player_ids(self) -> list[int]:
        return [p.user_id for p in self.active_players()]

    def submitted_count(self) -> int:
        return sum(1 for p in self.active_players() if p.action_for(self.round_number) is not None)

    def all_submitted(self) -> bool:
        active = self.active_players()
        return bool(active) and all(p.action_for(self.round_number) is not None for p in active)

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "host_id": self.host_id,
            "status": self.status.value,
            "round_number": self.round_number,
            "players": [p.to_dict() for p in self.players.values()],
            "round_started_at": _iso(self.round_started_at),
            "round_deadline": _iso(self.round_deadline),
            "settled_round": self.settled_round,
            "pending_result": self.pending_result,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }

    @classmethod
    def from_dict(cls, data: dict) -> DrinkingGame:
        players = [PlayerState.from_dict(p) for p in data["players"]]
        return cls(
            game_id=data["game_id"],
            guild_id=int(data["guild_id"]),
            channel_id=int(data["channel_id"]),
            message_id=int(data["message_id"]),
            host_id=int(data["host_id"]),
            status=GameStatus(data["status"]),
            round_number=int(data["round_number"]),
            players={p.user_id: p for p in players},
            round_started_at=_dt(data.get("round_started_at")),
            round_deadline=_dt(data.get("round_deadline")),
            settled_round=int(data.get("settled_round", 0)),
            pending_result=data.get("pending_result"),
            created_at=_dt(data["created_at"]),
            started_at=_dt(data.get("started_at")),
            finished_at=_dt(data.get("finished_at")),
        )

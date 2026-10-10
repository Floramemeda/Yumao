"""媚笙阁偷薯饼：地图、游戏与玩家状态。所有状态都可序列化为 JSON 以便重启恢复。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# ---------------------------------------------------------------- 地图

HALL = "hall"
KITCHEN = "kitchen"
CORRIDOR = "corridor"
SUITES = ["zhenfeng", "gehua", "manxue", "lianyue"]  # 二楼雅间

NODE_NAMES = {
    HALL: "大堂",
    KITCHEN: "后厨",
    CORRIDOR: "二楼走廊",
    "zhenfeng": "枕风阁",
    "gehua": "隔花阁",
    "manxue": "幔雪阁",
    "lianyue": "帘月阁",
}

PUBLIC_AREAS = {HALL, CORRIDOR}
ROOMS = {KITCHEN, *SUITES}
FLOOR = {HALL: 1, KITCHEN: 1, CORRIDOR: 2, **{s: 2 for s in SUITES}}
# 各楼层可搜查房间（按固定顺序，超时代选时取第一个可查的）
SEARCHABLE = {1: [KITCHEN], 2: list(SUITES)}

# 鼠的常规移动图（不含密道）；猫只能在大堂与二楼走廊之间上下楼
MOUSE_EDGES = {
    HALL: [CORRIDOR, KITCHEN],
    KITCHEN: [HALL],
    CORRIDOR: [HALL, *SUITES],
    **{s: [CORRIDOR] for s in SUITES},
}

# ---------------------------------------------------------------- 动作
# 动作以字符串表示，方便序列化和放进 Select 的 value：
#   猫：up / down / search:<room>
#   鼠：move:<node> / wait / steal
UP = "up"
DOWN = "down"
WAIT = "wait"
STEAL = "steal"
# 鼠超时专用：原地不动（公共区也适用，不能主动选择）
STAY = "stay"


def search(room: str) -> str:
    return f"search:{room}"


def move(node: str) -> str:
    return f"move:{node}"


def parse(action: str) -> tuple[str, str | None]:
    kind, _, arg = action.partition(":")
    return kind, (arg or None)


def describe(action: str) -> str:
    kind, arg = parse(action)
    if kind == "up":
        return "上楼到二楼走廊"
    if kind == "down":
        return "下楼到大堂"
    if kind == "search":
        return f"搜查{NODE_NAMES[arg]}"
    if kind == "move":
        return f"前往{NODE_NAMES[arg]}"
    if kind == "wait":
        return "原地等待"
    if kind == "steal":
        return "偷薯饼"
    if kind == "stay":
        return "原地不动"
    return action


# ---------------------------------------------------------------- 状态

class Role(str, Enum):
    CAT = "cat"
    MOUSE = "mouse"


class Status(str, Enum):
    LOBBY = "LOBBY"
    ROLE_ASSIGNMENT = "ROLE_ASSIGNMENT"
    ROUND_INPUT = "ROUND_INPUT"
    ROUND_RESOLUTION = "ROUND_RESOLUTION"      # 已结算、结果已落盘，尚未播报
    ROUND_ANNOUNCEMENT = "ROUND_ANNOUNCEMENT"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"                        # 报名超时无人开始


TERMINAL = {Status.FINISHED, Status.CANCELLED, Status.EXPIRED}


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class Player:
    user_id: int
    display_name: str
    role: Role | None = None
    position: str | None = None
    captured: bool = False
    captured_round: int | None = None

    # 行动按轮次隔离：只有 action_round == 当前轮次时 action 才有效
    action: str | None = None
    action_round: int = 0

    # 超时代选的私密提示
    auto_note: str | None = None
    auto_note_round: int = 0

    def action_for(self, round_no: int) -> str | None:
        return self.action if self.action_round == round_no else None

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "role": self.role.value if self.role else None,
            "position": self.position,
            "captured": self.captured,
            "captured_round": self.captured_round,
            "action": self.action,
            "action_round": self.action_round,
            "auto_note": self.auto_note,
            "auto_note_round": self.auto_note_round,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Player:
        return cls(
            user_id=int(d["user_id"]),
            display_name=d["display_name"],
            role=Role(d["role"]) if d.get("role") else None,
            position=d.get("position"),
            captured=bool(d.get("captured")),
            captured_round=d.get("captured_round"),
            action=d.get("action"),
            action_round=int(d.get("action_round", 0)),
            auto_note=d.get("auto_note"),
            auto_note_round=int(d.get("auto_note_round", 0)),
        )


@dataclass
class HeistGame:
    session_id: str
    guild_id: int
    channel_id: int
    message_id: int
    host_id: int
    status: Status
    created_at: datetime

    round_no: int = 0
    # dict 保持入局顺序；多鼠同时偷窃按入局顺序确定唯一携带者
    players: dict[int, Player] = field(default_factory=dict)

    secret_room: str | None = None
    # 房间 -> 最早可再次搜查的轮次（搜查轮 + 3）
    search_next_available_round: dict[str, int] = field(default_factory=dict)
    # 公开查房记录：[{round, room, captured: [user_id]}]
    search_log: list[dict] = field(default_factory=list)

    potato_carrier_id: int | None = None
    escaped_kitchen_at_round: int | None = None

    winner: Role | None = None
    win_reason: str | None = None

    deadline: datetime | None = None          # 报名截止或本轮行动截止
    resolved_round: int = 0                   # 已完成结算的最大轮次，防重复结算
    pending_result: dict | None = None        # 已结算未播报的结果，重启后据此补发

    started_at: datetime | None = None
    finished_at: datetime | None = None

    # ---- 便捷查询

    @property
    def cat(self) -> Player | None:
        return next((p for p in self.players.values() if p.role == Role.CAT), None)

    def mice(self) -> list[Player]:
        return [p for p in self.players.values() if p.role == Role.MOUSE]

    def alive_mice(self) -> list[Player]:
        return [p for p in self.mice() if not p.captured]

    def actors(self) -> list[Player]:
        """本轮需要行动的玩家：猫和未被捕的鼠。"""
        return [p for p in self.players.values() if p.role == Role.CAT or (p.role == Role.MOUSE and not p.captured)]

    def submitted_count(self) -> int:
        return sum(1 for p in self.actors() if p.action_for(self.round_no) is not None)

    def searchable(self, room: str) -> bool:
        return self.round_no >= self.search_next_available_round.get(room, 0)

    # ---- 序列化

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "host_id": self.host_id,
            "status": self.status.value,
            "created_at": _iso(self.created_at),
            "round_no": self.round_no,
            "players": [p.to_dict() for p in self.players.values()],
            "secret_room": self.secret_room,
            "search_next_available_round": self.search_next_available_round,
            "search_log": self.search_log,
            "potato_carrier_id": self.potato_carrier_id,
            "escaped_kitchen_at_round": self.escaped_kitchen_at_round,
            "winner": self.winner.value if self.winner else None,
            "win_reason": self.win_reason,
            "deadline": _iso(self.deadline),
            "resolved_round": self.resolved_round,
            "pending_result": self.pending_result,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }

    @classmethod
    def from_dict(cls, d: dict) -> HeistGame:
        players = [Player.from_dict(p) for p in d["players"]]
        return cls(
            session_id=d["session_id"],
            guild_id=int(d["guild_id"]),
            channel_id=int(d["channel_id"]),
            message_id=int(d["message_id"]),
            host_id=int(d["host_id"]),
            status=Status(d["status"]),
            created_at=_dt(d["created_at"]),
            round_no=int(d.get("round_no", 0)),
            players={p.user_id: p for p in players},
            secret_room=d.get("secret_room"),
            search_next_available_round={k: int(v) for k, v in d.get("search_next_available_round", {}).items()},
            search_log=d.get("search_log", []),
            potato_carrier_id=d.get("potato_carrier_id"),
            escaped_kitchen_at_round=d.get("escaped_kitchen_at_round"),
            winner=Role(d["winner"]) if d.get("winner") else None,
            win_reason=d.get("win_reason"),
            deadline=_dt(d.get("deadline")),
            resolved_round=int(d.get("resolved_round", 0)),
            pending_result=d.get("pending_result"),
            started_at=_dt(d.get("started_at")),
            finished_at=_dt(d.get("finished_at")),
        )

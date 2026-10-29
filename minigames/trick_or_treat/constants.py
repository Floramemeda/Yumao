"""不给糖就捣蛋：固定规则常量。

部位只存稳定 ID；活动开始后不得改动列表或语义，否则既有排除记录和藏糖位置会失效。
"""
from __future__ import annotations

BODY_PARTS: list[tuple[str, str]] = [
    ("mouth", "嘴巴里"),
    ("chest", "胸口"),
    ("armpit", "腋下"),
    ("behind_ear", "耳后"),
    ("underwear", "内裤里"),
    ("butt_crack", "屁股缝"),
    ("sock", "袜子里"),
]
PART_IDS = [pid for pid, _ in BODY_PARTS]
PART_LABELS = dict(BODY_PARTS)

ATTEMPTS_PER_PLAYER = 3
NICKNAME_MAX_LENGTH = 20

# 活动状态
REGISTRATION = "REGISTRATION"
ACTIVE = "ACTIVE"
CLOSED = "CLOSED"
STATE_LABELS = {REGISTRATION: "报名中", ACTIVE: "进行中", CLOSED: "已结束"}

# 玩家状态
JOINED = "joined"
FOUND = "found"
WITHDRAWN = "withdrawn"


def label(part_id: str) -> str:
    return PART_LABELS.get(part_id, part_id)

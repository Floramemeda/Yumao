"""撸猫续火花：签到、连续火花、亲密等级、催撸判定。纯逻辑，不依赖 Discord。

所有"今天/昨天"都是 Guild 本地日期，由调用方按配置时区算好传入。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

SPARK_MIN_STREAK = 2  # 连续 2 天点亮火花


@dataclass
class Progress:
    guild_id: int
    user_id: int
    last_checkin_local_date: date | None = None
    streak_count: int = 0          # 最后一次有效签到时的连续天数
    best_streak: int = 0
    total_checkin_days: int = 0
    reminder_opt_out: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "last_checkin_local_date": self.last_checkin_local_date.isoformat() if self.last_checkin_local_date else None,
            "streak_count": self.streak_count,
            "best_streak": self.best_streak,
            "total_checkin_days": self.total_checkin_days,
            "reminder_opt_out": self.reminder_opt_out,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Progress:
        last = d.get("last_checkin_local_date")
        return cls(
            guild_id=int(d["guild_id"]),
            user_id=int(d["user_id"]),
            last_checkin_local_date=date.fromisoformat(last) if last else None,
            streak_count=int(d.get("streak_count", 0)),
            best_streak=int(d.get("best_streak", 0)),
            total_checkin_days=int(d.get("total_checkin_days", 0)),
            reminder_opt_out=bool(d.get("reminder_opt_out", False)),
            created_at=datetime.fromisoformat(d["created_at"]) if d.get("created_at") else None,
            updated_at=datetime.fromisoformat(d["updated_at"]) if d.get("updated_at") else None,
        )


@dataclass
class Level:
    level: int
    name: str
    emoji: str
    threshold: int
    description: str = ""


def parse_levels(raw: list[dict]) -> list[Level]:
    levels = [Level(int(x["level"]), x["name"], x.get("emoji", ""), int(x["threshold"]), x.get("description", ""))
              for x in raw]
    return sorted(levels, key=lambda lv: lv.threshold)


def level_for(total_days: int, levels: list[Level]) -> Level | None:
    """亲密等级只看累计有效签到天数；未达到最低门槛返回 None。"""
    current = None
    for lv in levels:
        if total_days >= lv.threshold:
            current = lv
    return current


def next_level(total_days: int, levels: list[Level]) -> Level | None:
    return next((lv for lv in levels if lv.threshold > total_days), None)


def current_streak(p: Progress, today: date) -> int:
    """从最后签到日期派生真实的当前连续天数：最后签到早于昨天即视为已断。"""
    last = p.last_checkin_local_date
    if last is None or last < today - timedelta(days=1):
        return 0
    return p.streak_count


def spark_active(p: Progress, today: date) -> bool:
    return current_streak(p, today) >= SPARK_MIN_STREAK


@dataclass
class CheckinResult:
    counted: bool            # 是否为今天的首次有效签到
    first_ever: bool = False
    spark_activated: bool = False  # 本次签到恰好达到连续 2 天
    comeback: bool = False   # 断签后重新签到（以前签过、但最后签到早于昨天）
    level_up: bool = False


def checkin(p: Progress, today: date, levels: list[Level], now: datetime | None = None) -> CheckinResult:
    """执行签到（同一本地日重复调用是幂等的）。会修改 p。"""
    last = p.last_checkin_local_date
    if last == today:
        return CheckinResult(counted=False)

    before_level = level_for(p.total_checkin_days, levels)
    first_ever = last is None
    comeback = last is not None and last < today - timedelta(days=1)

    p.streak_count = p.streak_count + 1 if last == today - timedelta(days=1) else 1
    p.total_checkin_days += 1
    p.best_streak = max(p.best_streak, p.streak_count)
    p.last_checkin_local_date = today
    p.updated_at = now
    if p.created_at is None:
        p.created_at = now

    after_level = level_for(p.total_checkin_days, levels)
    return CheckinResult(
        counted=True,
        first_ever=first_ever,
        spark_activated=p.streak_count == SPARK_MIN_STREAK,
        comeback=comeback,
        level_up=after_level is not None and after_level != before_level,
    )


def should_remind(p: Progress, today: date) -> bool:
    """22:00 催撸条件：昨天签到且当时已连续 ≥2 天、今天还没签、没有关闭催撸。

    （"今天是否已催过"由调用方用持久化记录判断。）
    """
    return (not p.reminder_opt_out
            and p.last_checkin_local_date == today - timedelta(days=1)
            and p.streak_count >= SPARK_MIN_STREAK)

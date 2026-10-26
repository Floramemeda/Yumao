"""RP 场次：生命周期、权限复查、玩家状态、异常、进度、审计事件与公告 outbox。不依赖 Discord。

DM 对剧情和数值拥有最终裁定权；这里只做记录与校验。所有修改在 Guild 锁内基于最新数据进行，
每次实际变更写一条审计事件，并把待发公告放进场次的 outbox——公告发送失败不会撤销或重复变更。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..common import PROJECT_ROOT
from .store import RpStore, StorageError

ACTIVE = "ACTIVE"
ENDED = "ENDED"
STATS = {"sanity": "理智", "heat": "发情值"}


class RpError(Exception):
    """可直接展示给用户的错误。"""


@dataclass(frozen=True)
class Actor:
    user_id: int
    is_admin: bool = False      # 管理服务器 / Administrator


def load_config() -> dict:
    with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
        return json.load(f)


def parse_stat_input(text: str | None) -> tuple[str, int] | None:
    """"" → 不改；"+20" / "-15" → 增减；"80" / "=80" → 设为绝对值。"""
    s = (text or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"([+-])\s*(\d{1,6})", s)
    if m:
        n = int(m.group(2))
        return ("delta", n if m.group(1) == "+" else -n)
    m = re.fullmatch(r"=?\s*(\d{1,6})", s)
    if m:
        return ("set", int(m.group(1)))
    raise RpError(f"看不懂「{s[:20]}」：用 +20 / -15 表示增减，直接写 80 表示设为 80。")


class RpService:
    def __init__(self, cfg: dict | None = None, store: RpStore | None = None, clock=None):
        self.cfg = cfg or load_config()
        self.store = store or RpStore(PROJECT_ROOT / self.cfg["data_dir"])
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> str:
        return self.clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------ 自动骰点频道

    def dm_role_ids(self, guild_id: int) -> set[int]:
        return {int(r) for r in self.cfg.get("guilds", {}).get(str(guild_id), {}).get("dm_role_ids", [])}

    def config_roll_channels(self, guild_id: int) -> set[int]:
        """config.json 里固定开启的自动骰点频道（不能用 /roll-config 关闭）。"""
        return {int(c) for c in self.cfg.get("guilds", {}).get(str(guild_id), {}).get("roll_channel_ids", [])}

    def is_roll_channel(self, guild_id: int, channel_id: int, parent_id: int | None) -> bool:
        """频道本身已启用，或它是已启用父频道下的子区（继承）。配置文件与 /roll-config 两种来源都算。"""
        enabled = {str(c) for c in self.config_roll_channels(guild_id)} | set(self.store.load(guild_id)["roll_channels"])
        return str(channel_id) in enabled or (parent_id is not None and str(parent_id) in enabled)

    async def set_roll_channel(self, guild_id: int, channel_id: int, enabled: bool, actor_id: int) -> bool:
        """返回是否实际发生变化。"""
        async with self.store.transaction(guild_id) as doc:
            key = str(channel_id)
            if enabled == (key in doc["roll_channels"]):
                return False
            if enabled:
                doc["roll_channels"][key] = {"configured_by": str(actor_id), "updated_at": self._now()}
            else:
                del doc["roll_channels"][key]
            return True

    def roll_channels(self, guild_id: int) -> list[int]:
        return [int(c) for c in self.store.load(guild_id)["roll_channels"]]

    # ------------------------------------------------------------------ 场次查询

    @staticmethod
    def _active_in(doc: dict, channel_id: int) -> dict | None:
        return next((s for s in doc["sessions"].values()
                     if s["channel_id"] == str(channel_id) and s["status"] == ACTIVE), None)

    def active_session(self, guild_id: int, channel_id: int) -> dict | None:
        s = self._active_in(self.store.load(guild_id), channel_id)
        return s and json.loads(json.dumps(s))

    def require_active(self, guild_id: int, channel_id: int) -> dict:
        s = self.active_session(guild_id, channel_id)
        if s is None:
            raise RpError("这个频道现在没有进行中的探索。")
        return s

    @staticmethod
    def can_manage(session: dict, actor: Actor) -> bool:
        return actor.is_admin or session["dm_user_id"] == str(actor.user_id)

    @staticmethod
    def active_players(session: dict) -> list[tuple[str, dict]]:
        rows = [(uid, p) for uid, p in session["participants"].items() if p["active"]]
        return sorted(rows, key=lambda x: x[1]["joined_at"])

    # ------------------------------------------------------------------ 修改（统一入口）

    def _locate(self, doc: dict, session_id: str, actor: Actor) -> dict:
        """锁内复查：场次存在、仍进行中、操作者是 DM 或管理员。不信任任何组件里带来的状态。"""
        s = doc["sessions"].get(session_id)
        if s is None:
            raise RpError("找不到这场探索。")
        if s["status"] != ACTIVE:
            raise RpError("这场探索已经结束了。")
        if not self.can_manage(s, actor):
            raise RpError("只有本场 DM 或管理员可以操作。")
        return s

    def _event(self, s: dict, actor: Actor, etype: str, target: str | None, before, after, metadata=None,
               announce: dict | None = None) -> dict:
        ev = {"id": uuid.uuid4().hex[:12], "type": etype, "actor_user_id": str(actor.user_id),
              "target_user_id": target, "before": before, "after": after, "metadata": metadata or {},
              "created_at": self._now(), "announcement_message_id": None}
        s["events"].append(ev)
        s["version"] += 1
        if announce is not None:
            s["pending_announcements"].append({"event_id": ev["id"], "actor": str(actor.user_id), **announce})
        return ev

    @staticmethod
    def _player(s: dict, user_id) -> dict:
        p = s["participants"].get(str(user_id))
        if p is None or not p["active"]:
            raise RpError("这位玩家不在本场探索中。")
        return p

    # ------------------------------------------------------------------ 生命周期

    async def start(self, guild_id: int, channel_id: int, dm_id: int, progress_max: int | None) -> dict:
        pm = progress_max if progress_max is not None else self.cfg["default_progress_max"]
        if not 1 <= pm <= self.cfg["max_progress_max"]:
            raise RpError(f"总进度要在 1–{self.cfg['max_progress_max']} 之间。")
        async with self.store.transaction(guild_id) as doc:
            if self._active_in(doc, channel_id):
                raise RpError("这个频道已经有一场进行中的探索了。")
            sid = uuid.uuid4().hex[:12]
            s = {"id": sid, "guild_id": str(guild_id), "channel_id": str(channel_id), "dm_user_id": str(dm_id),
                 "status": ACTIVE, "progress": 0, "progress_max": pm, "created_at": self._now(), "ended_at": None,
                 "version": 0, "participants": {}, "events": [], "pending_announcements": []}
            doc["sessions"][sid] = s
            self._event(s, Actor(dm_id), "SESSION_START", None, None, {"progress_max": pm})
        return json.loads(json.dumps(s))

    async def end(self, guild_id: int, session_id: str, actor: Actor) -> dict:
        async with self.store.transaction(guild_id) as doc:
            s = self._locate(doc, session_id, actor)
            s["status"] = ENDED
            s["ended_at"] = self._now()
            self._event(s, actor, "SESSION_END", None, ACTIVE, ENDED, announce={"kind": "end"})
        return json.loads(json.dumps(s))

    # ------------------------------------------------------------------ 玩家

    async def add_players(self, guild_id: int, session_id: str, actor: Actor, user_ids: list[int]) -> list[str]:
        async with self.store.transaction(guild_id) as doc:
            s = self._locate(doc, session_id, actor)
            added = []
            for uid in map(str, dict.fromkeys(user_ids)):
                p = s["participants"].get(uid)
                if p is not None and p["active"]:
                    continue
                if len(self.active_players(s)) >= self.cfg["max_players"]:
                    raise RpError(f"一场最多 {self.cfg['max_players']} 名玩家。")
                if p is None:   # 新玩家：默认数值
                    p = {"sanity": self.cfg["initial_sanity"], "heat": self.cfg["initial_heat"], "conditions": [],
                         "active": True, "joined_at": self._now(), "left_at": None}
                    s["participants"][uid] = p
                else:           # 曾被移除：恢复原记录
                    p.update(active=True, left_at=None, joined_at=self._now())
                self._event(s, actor, "PARTICIPANT_ADD", uid, None, {"sanity": p["sanity"], "heat": p["heat"]})
                added.append(uid)
        return added

    async def remove_player(self, guild_id: int, session_id: str, actor: Actor, user_id: int) -> None:
        async with self.store.transaction(guild_id) as doc:
            s = self._locate(doc, session_id, actor)
            p = self._player(s, user_id)
            p.update(active=False, left_at=self._now())
            self._event(s, actor, "PARTICIPANT_REMOVE", str(user_id), True, False)

    # ------------------------------------------------------------------ 数值

    async def change_stats(self, guild_id: int, session_id: str, actor: Actor, user_id: int,
                           inputs: dict[str, str | None]) -> dict | None:
        """inputs: {"sanity": "-15", "heat": "+20"}。增减按范围夹取；绝对值越界直接拒绝。无变化返回 None。"""
        lo, hi = self.cfg["stat_min"], self.cfg["stat_max"]
        parsed = {k: parse_stat_input(v) for k, v in inputs.items() if k in STATS}
        for key, cmd in parsed.items():
            if cmd and cmd[0] == "set" and not lo <= cmd[1] <= hi:
                raise RpError(f"{STATS[key]}只能设为 {lo}–{hi}。")
        async with self.store.transaction(guild_id) as doc:
            s = self._locate(doc, session_id, actor)
            p = self._player(s, user_id)
            changes = {}
            for key, cmd in parsed.items():
                if cmd is None:
                    continue
                before = p[key]
                after = max(lo, min(hi, before + cmd[1])) if cmd[0] == "delta" else cmd[1]
                if after != before:
                    p[key] = after
                    changes[key] = [before, after]
                    self._event(s, actor, "STAT_DELTA" if cmd[0] == "delta" else "STAT_SET", str(user_id),
                                before, after, {"stat": key, "input": cmd[1]})
            if not changes:
                return None
            s["pending_announcements"].append({"event_id": s["events"][-1]["id"], "actor": str(actor.user_id),
                                               "kind": "player", "user_id": str(user_id), "changes": changes,
                                               "condition_change": None})
        return {"changes": changes}

    # ------------------------------------------------------------------ 异常状态

    def _condition_name(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise RpError("异常状态名称不能为空。")
        if len(name) > self.cfg["condition_max_length"]:
            raise RpError(f"异常状态名称最多 {self.cfg['condition_max_length']} 个字。")
        return name

    async def add_condition(self, guild_id: int, session_id: str, actor: Actor, user_id: int, name: str) -> None:
        name = self._condition_name(name)
        async with self.store.transaction(guild_id) as doc:
            s = self._locate(doc, session_id, actor)
            p = self._player(s, user_id)
            if any(c["name"] == name for c in p["conditions"]):
                raise RpError(f"这位玩家已经有「{name}」了。")
            p["conditions"].append({"name": name, "created_by": str(actor.user_id), "created_at": self._now()})
            self._event(s, actor, "CONDITION_ADD", str(user_id), None, name,
                        announce={"kind": "player", "user_id": str(user_id), "changes": {},
                                  "condition_change": ["+", name]})

    async def remove_condition(self, guild_id: int, session_id: str, actor: Actor, user_id: int, name: str) -> None:
        async with self.store.transaction(guild_id) as doc:
            s = self._locate(doc, session_id, actor)
            p = self._player(s, user_id)
            match = next((c for c in p["conditions"] if c["name"] == name), None)
            if match is None:
                raise RpError(f"这位玩家没有「{name}」。")
            p["conditions"].remove(match)
            self._event(s, actor, "CONDITION_REMOVE", str(user_id), name, None,
                        announce={"kind": "player", "user_id": str(user_id), "changes": {},
                                  "condition_change": ["-", name]})

    # ------------------------------------------------------------------ 进度

    async def change_progress(self, guild_id: int, session_id: str, actor: Actor, *, delta: int | None = None,
                              set_to: int | None = None) -> list[int] | None:
        async with self.store.transaction(guild_id) as doc:
            s = self._locate(doc, session_id, actor)
            before, pm = s["progress"], s["progress_max"]
            if set_to is not None:
                if not 0 <= set_to <= pm:
                    raise RpError(f"进度只能设为 0–{pm}。")
                after = set_to
            else:
                after = max(0, min(pm, before + (delta or 0)))
            if after == before:
                return None
            s["progress"] = after
            self._event(s, actor, "PROGRESS_SET", None, before, after,
                        announce={"kind": "progress", "before": before, "after": after, "max": pm})
        return [before, after]

    # ------------------------------------------------------------------ 公告 outbox

    def pending(self, guild_id: int, session_id: str) -> list[dict]:
        s = self.store.load(guild_id)["sessions"].get(session_id)
        return list(s["pending_announcements"]) if s else []

    async def mark_announced(self, guild_id: int, session_id: str, event_id: str, message_id: int) -> None:
        async with self.store.transaction(guild_id) as doc:
            s = doc["sessions"][session_id]
            s["pending_announcements"] = [a for a in s["pending_announcements"] if a["event_id"] != event_id]
            for ev in s["events"]:
                if ev["id"] == event_id:
                    ev["announcement_message_id"] = str(message_id)


__all__ = ["RpService", "RpError", "Actor", "StorageError", "ACTIVE", "ENDED", "STATS", "parse_stat_input"]

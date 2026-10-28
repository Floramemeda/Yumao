"""不给糖就捣蛋：活动生命周期、报名、搜查事务、配对推导、审计与公告 outbox。不依赖 Discord。

所有修改都在 Guild 锁内基于最新数据重新校验；校验不通过直接抛 TotError，整笔事务丢弃，
所以被拒绝的请求永远不会扣次数。合法搜查在同一笔事务里扣次数、记录搜查、写归属并入队公告；
公告发送失败只留在 outbox 里补发，不回滚搜查，也不给免费重搜。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..common import PROJECT_ROOT
from .constants import (ACTIVE, ATTEMPTS_PER_PLAYER, CLOSED, FOUND, JOINED, PART_IDS, REGISTRATION,
                        WITHDRAWN, label)
from .store import StorageError, TotStore


class TotError(Exception):
    """可直接展示给用户的错误。"""


def load_config() -> dict:
    with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 纯函数：从事件事实推导状态

def hits(event: dict) -> list[dict]:
    return [s for s in event["searches"] if s["hit"]]


def matched_ids(event: dict) -> set[str]:
    """找到过别人或被别人找到的玩家（可多重配对）。"""
    out = set()
    for s in hits(event):
        out.update((s["searcher"], s["target"]))
    return out


def excluded_parts(event: dict, target: str) -> list[str]:
    """目标已被公开排除（搜过但没中）的部位，按部位表顺序。"""
    missed = {s["part"] for s in event["searches"] if s["target"] == target and not s["hit"]}
    return [p for p in PART_IDS if p in missed]


def active_players(event: dict) -> dict[str, dict]:
    return {uid: p for uid, p in event["players"].items() if p["state"] != WITHDRAWN}


def can_continue(event: dict) -> bool:
    """是否还存在「有次数的人 × 可被搜的别人」组合；否则可以结算。"""
    players = active_players(event)
    searchers = [uid for uid, p in players.items() if p["attempts_remaining"] > 0]
    targets = [uid for uid, p in players.items() if p["state"] == JOINED]
    return any(s != t for s in searchers for t in targets)


def stats(event: dict) -> dict:
    players = active_players(event)
    matched = matched_ids(event) & set(players)
    total = event["config"]["attempts_per_player"]
    found_counts: dict[str, int] = {}
    for s in hits(event):
        found_counts[s["searcher"]] = found_counts.get(s["searcher"], 0) + 1
    top = max(found_counts.values(), default=0)
    return {
        "registered": len(players),
        "withdrawn": len(event["players"]) - len(players),
        "unfound": sorted(uid for uid, p in players.items() if p["state"] == JOINED),
        "found": len(hits(event)),
        "matched": len(matched),
        "unmatched": len(players) - len(matched),
        "attempts_used": sum(total - p["attempts_remaining"] for p in players.values()),
        "attempts_total": total * len(players),
        "hunters": sorted(uid for uid, n in found_counts.items() if n == top) if top else [],
        "hunter_count": top,
        "exclusions": {uid: excluded_parts(event, uid) for uid, p in players.items()
                       if p["state"] == JOINED and excluded_parts(event, uid)},
        "can_continue": can_continue(event),
    }


def player_view(event: dict, uid: str) -> dict | None:
    """玩家本人的状态（含藏糖位置，只能给本人看）。"""
    p = event["players"].get(uid)
    if p is None:
        return None
    return {
        **p,
        "attempts_total": event["config"]["attempts_per_player"],
        "candies": [s["target"] for s in hits(event) if s["searcher"] == uid],
        "matched": uid in matched_ids(event),
        "event_state": event["state"],
    }


class TotService:
    def __init__(self, cfg: dict | None = None, store: TotStore | None = None, clock=None):
        self.cfg = cfg or load_config()
        self.store = store or TotStore(PROJECT_ROOT / self.cfg["data_dir"])
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> str:
        return self.clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _next_id(doc: dict, prefix: str) -> str:
        doc["seq"] += 1
        return f"{prefix}{doc['seq']}"

    def _audit(self, doc: dict, event_id: str | None, actor_id: int, action: str, **meta) -> None:
        """只记操作元数据，绝不写入未公开的藏糖位置。"""
        doc["audit"].append({"id": self._next_id(doc, "a"), "event_id": event_id, "actor": str(actor_id),
                             "action": action, "meta": meta, "at": self._now()})

    def _enqueue(self, doc: dict, event: dict, kind: str, payload: dict, search_id: str | None = None) -> None:
        event["outbox"].append({"id": self._next_id(doc, "o"), "kind": kind, "search_id": search_id,
                                "payload": payload, "status": "pending", "attempts": 0, "last_error": None,
                                "message_id": None, "created_at": self._now(), "sent_at": None})

    # ------------------------------------------------------------------ 读取

    def settings(self, guild_id: int) -> dict:
        return self.store.load(guild_id)["settings"]

    def current_event(self, guild_id: int) -> dict | None:
        doc = self.store.load(guild_id)
        eid = doc["current_event_id"]
        return doc["events"].get(eid) if eid else None

    def event(self, guild_id: int, event_id: str) -> dict | None:
        return self.store.load(guild_id)["events"].get(event_id)

    @staticmethod
    def _current(doc: dict) -> dict:
        eid = doc["current_event_id"]
        if not eid:
            raise TotError("现在没有进行中的万圣夜活动。")
        return doc["events"][eid]

    @staticmethod
    def _locate(doc: dict, event_id: str) -> dict:
        event = doc["events"].get(event_id)
        if event is None:
            raise TotError("找不到这一轮活动。")
        if event["state"] == CLOSED:
            raise TotError("这一轮万圣夜活动已经结束了。")
        return event

    # ------------------------------------------------------------------ 管理

    async def setup(self, guild_id: int, entry_channel_id: int, announce_channel_id: int, actor_id: int) -> None:
        async with self.store.transaction(guild_id) as doc:
            doc["settings"]["entry_channel_id"] = str(entry_channel_id)
            doc["settings"]["announcement_channel_id"] = str(announce_channel_id)
            self._audit(doc, None, actor_id, "SETUP", entry=str(entry_channel_id), announce=str(announce_channel_id))

    async def set_late_join(self, guild_id: int, value: bool, actor_id: int) -> None:
        async with self.store.transaction(guild_id) as doc:
            doc["settings"]["allow_late_join"] = value
            eid = doc["current_event_id"]
            if eid:
                doc["events"][eid]["config"]["allow_late_join"] = value
            self._audit(doc, eid, actor_id, "CONFIG", allow_late_join=value)

    async def open_event(self, guild_id: int, actor_id: int) -> dict:
        async with self.store.transaction(guild_id) as doc:
            st = doc["settings"]
            if not st.get("entry_channel_id") or not st.get("announcement_channel_id"):
                raise TotError("请先用 /trickortreat_admin setup 设置报名入口和公告频道。")
            if doc["current_event_id"]:
                raise TotError("本服已经有一轮未结束的活动，请先 /trickortreat_admin close。")
            eid = self._next_id(doc, "e")
            event = {
                "id": eid, "state": REGISTRATION,
                "config": {"attempts_per_player": ATTEMPTS_PER_PLAYER, "body_parts": list(PART_IDS),
                           "allow_late_join": st.get("allow_late_join", self.cfg["allow_late_join_default"])},
                "entry_channel_id": st["entry_channel_id"], "announcement_channel_id": st["announcement_channel_id"],
                "panel_channel_id": None, "panel_message_id": None,
                "created_at": self._now(), "started_at": None, "closed_at": None,
                "players": {}, "searches": [], "outbox": [],
            }
            doc["events"][eid] = event
            doc["current_event_id"] = eid
            self._audit(doc, eid, actor_id, "OPEN")
        return event

    async def set_panel(self, guild_id: int, event_id: str, channel_id: int, message_id: int) -> None:
        async with self.store.transaction(guild_id) as doc:
            event = doc["events"][event_id]
            event["panel_channel_id"], event["panel_message_id"] = str(channel_id), str(message_id)

    async def start(self, guild_id: int, actor_id: int) -> dict:
        async with self.store.transaction(guild_id) as doc:
            event = self._current(doc)
            if event["state"] != REGISTRATION:
                raise TotError("活动已经开始了。")
            event["state"], event["started_at"] = ACTIVE, self._now()
            self._audit(doc, event["id"], actor_id, "START")
        return event

    async def close(self, guild_id: int, actor_id: int) -> dict:
        """关闭本轮并把最终结果放进 outbox。之后所有报名 / 搜查都会被拒绝。"""
        async with self.store.transaction(guild_id) as doc:
            event = self._current(doc)
            event["state"], event["closed_at"] = CLOSED, self._now()
            doc["current_event_id"] = None
            st = stats(event)
            self._enqueue(doc, event, "result", {k: st[k] for k in (
                "registered", "withdrawn", "found", "matched", "unmatched", "hunters", "hunter_count", "unfound")})
            self._audit(doc, event["id"], actor_id, "CLOSE", found=st["found"], matched=st["matched"])
        return event

    async def restore(self, guild_id: int, user_id: int, actor_id: int) -> dict:
        """管理员人工恢复退出者：不重置次数、不改藏糖位置。"""
        async with self.store.transaction(guild_id) as doc:
            event = self._current(doc)
            p = event["players"].get(str(user_id))
            if p is None or p["state"] != WITHDRAWN:
                raise TotError("这位玩家没有处于退出状态。")
            p["state"] = FOUND if p["found_by"] else JOINED
            p["withdrawn_at"] = None
            self._audit(doc, event["id"], actor_id, "RESTORE", user=str(user_id))
        return p

    # ------------------------------------------------------------------ 报名 / 退出

    @staticmethod
    def _check_join(event: dict, uid: str) -> None:
        p = event["players"].get(uid)
        if p is not None:
            if p["state"] == WITHDRAWN:
                raise TotError("你已经退出了本轮活动，如需恢复请联系管理员。")
            raise TotError("你已经报名过了，糖果藏好了哦～用「📋 我的状态」查看。")
        if event["state"] == ACTIVE and not event["config"]["allow_late_join"]:
            raise TotError("活动已经开始，本轮不接受补报。")

    def check_can_join(self, guild_id: int, event_id: str, user_id: int) -> None:
        """打开部位选择前的预检（提交时还会再检一次）。"""
        self._check_join(self._locate(self.store.load(guild_id), event_id), str(user_id))

    async def join(self, guild_id: int, event_id: str, user_id: int, part: str) -> dict:
        if part not in PART_IDS:
            raise TotError("没有这个藏糖部位。")
        async with self.store.transaction(guild_id) as doc:
            event = self._locate(doc, event_id)
            uid = str(user_id)
            self._check_join(event, uid)
            p = {"hiding_part": part, "attempts_remaining": event["config"]["attempts_per_player"],
                 "state": JOINED, "found_by": None, "joined_at": self._now(), "found_at": None,
                 "withdrawn_at": None}
            event["players"][uid] = p
        return p

    async def withdraw(self, guild_id: int, user_id: int) -> None:
        async with self.store.transaction(guild_id) as doc:
            event = self._current(doc)
            p = event["players"].get(str(user_id))
            if p is None:
                raise TotError("你还没有报名。")
            if p["state"] == WITHDRAWN:
                raise TotError("你已经退出了。")
            p["state"], p["withdrawn_at"] = WITHDRAWN, self._now()
            self._audit(doc, event["id"], user_id, "WITHDRAW")

    # ------------------------------------------------------------------ 搜查

    async def search(self, guild_id: int, searcher_id: int, target_id: int, part: str,
                     interaction_id: int) -> tuple[dict, dict, bool]:
        """一次原子搜查。返回 (搜查记录, 搜查者玩家记录, 是否为重复投递的回放)。"""
        async with self.store.transaction(guild_id) as doc:
            event = self._current(doc)
            for s in event["searches"]:
                if s["interaction_id"] == str(interaction_id):        # 同一交互重复送达：不再扣次数
                    return s, event["players"][s["searcher"]], True
            if event["state"] != ACTIVE:
                raise TotError("活动还没开始搜查，请等管理员宣布开始。")
            sid, tid = str(searcher_id), str(target_id)
            me = event["players"].get(sid)
            if me is None or me["state"] == WITHDRAWN:
                raise TotError("你还没有报名本轮活动（或已退出），不能搜查。")
            if me["attempts_remaining"] <= 0:
                raise TotError("你的搜查次数已经用完了。不过你的糖还能被别人找哦。")
            if sid == tid:
                raise TotError("不能搜自己啦。")
            target = event["players"].get(tid)
            if target is None or target["state"] == WITHDRAWN:
                raise TotError("对方没有报名本轮活动（或已退出），不能搜查。")
            if target["state"] == FOUND:
                raise TotError("对方的糖已经被别人找到了。")
            if part not in event["config"]["body_parts"]:
                raise TotError("没有这个部位。")
            if part in excluded_parts(event, tid):
                raise TotError(f"对方的「{label(part)}」已经被搜过了，换个地方吧。")

            hit = target["hiding_part"] == part
            me["attempts_remaining"] -= 1
            record = {"id": self._next_id(doc, "s"), "interaction_id": str(interaction_id), "searcher": sid,
                      "target": tid, "part": part, "hit": hit, "at": self._now()}
            event["searches"].append(record)
            if hit:
                target["state"], target["found_by"], target["found_at"] = FOUND, sid, record["at"]
            self._enqueue(doc, event, "search", {
                "searcher": sid, "target": tid, "part": part, "hit": hit,
                "excluded": excluded_parts(event, tid), "remaining": me["attempts_remaining"],
                "total": event["config"]["attempts_per_player"]}, search_id=record["id"])
        return record, me, False

    # ------------------------------------------------------------------ 公告 outbox

    def pending(self, guild_id: int) -> list[tuple[dict, dict]]:
        """所有轮次里待发的公告 (活动, 公告)，按入队顺序。"""
        doc = self.store.load(guild_id)
        items = [(e, o) for e in doc["events"].values() for o in e["outbox"] if o["status"] == "pending"]
        return sorted(items, key=lambda eo: int(eo[1]["id"][1:]))

    def failed(self, guild_id: int) -> list[tuple[dict, dict]]:
        doc = self.store.load(guild_id)
        return [(e, o) for e in doc["events"].values() for o in e["outbox"] if o["status"] == "failed"]

    async def _update_outbox(self, guild_id: int, event_id: str, outbox_id: str, fn) -> None:
        async with self.store.transaction(guild_id) as doc:
            for o in doc["events"][event_id]["outbox"]:
                if o["id"] == outbox_id:
                    fn(o)

    async def mark_attempt(self, guild_id: int, event_id: str, outbox_id: str) -> None:
        """发送前先记一次尝试：若之后进程在「已发出、未记录」之间崩溃，补发前会先核对频道历史。"""
        def f(o):
            o["attempts"] += 1
        await self._update_outbox(guild_id, event_id, outbox_id, f)

    async def mark_sent(self, guild_id: int, event_id: str, outbox_id: str, message_id: int) -> None:
        def f(o):
            o["status"], o["message_id"], o["sent_at"], o["last_error"] = "sent", str(message_id), self._now(), None
        await self._update_outbox(guild_id, event_id, outbox_id, f)

    async def mark_error(self, guild_id: int, event_id: str, outbox_id: str, error: str) -> None:
        def f(o):
            o["last_error"] = error[:200]
            if o["attempts"] >= self.cfg["outbox_max_retries"]:
                o["status"] = "failed"
        await self._update_outbox(guild_id, event_id, outbox_id, f)

    async def requeue_failed(self, guild_id: int, actor_id: int) -> int:
        async with self.store.transaction(guild_id) as doc:
            n = 0
            for e in doc["events"].values():
                for o in e["outbox"]:
                    if o["status"] == "failed":
                        o["status"] = "pending"
                        n += 1
            if n:
                self._audit(doc, None, actor_id, "REQUEUE_ANNOUNCEMENTS", count=n)
        return n


__all__ = ["TotService", "TotError", "StorageError", "load_config", "stats", "player_view", "matched_ids",
           "excluded_parts", "can_continue", "hits"]

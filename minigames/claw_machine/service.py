"""万象娃娃机：服务层（不依赖 Discord 的状态变更都在这里，全部走 store 事务）。

保证：
- 一个 EVENT_ID 只对应一局（doc["events"] 唯一键，事务内检查）。
- 一局只结算一次、最多产生一只娃娃（局里的 result 一旦写入，重复请求只返回已保存的结果）。
- 娃娃同时只有一个持有者；同一只娃娃同时只能有一个待处理赠礼（doll["pending_gift"] 加锁）。
- 所有判定只用服务端保存的候选词条和配置里的成功率，不信任客户端传来的任何数值。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..common import PROJECT_ROOT
from . import engine
from .store import ClawStore

PENDING, DONE = "pending", "done"
GIFT_PENDING, GIFT_ACCEPTED, GIFT_DECLINED, GIFT_CANCELLED, GIFT_EXPIRED = (
    "pending", "accepted", "declined", "cancelled", "expired")
FILTERS = ("all", "eggs", "showcase")


class ClawError(Exception):
    """可以直接展示给玩家的业务错误。"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _new_stats() -> dict:
    return {"plays": 0, "wins": 0, "combos": [], "eggs": [], "gifts_sent": 0, "gifts_received": 0}


class ClawService:
    def __init__(self, cfg: dict | None = None, store: ClawStore | None = None, rng=None, clock=None):
        self.cfg = cfg or engine.load_config()
        self.store = store or ClawStore(PROJECT_ROOT / Path(self.cfg["data_dir"]))
        self.rng = rng or secrets.SystemRandom()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self) -> datetime:
        return self.clock()

    # ------------------------------------------------------------------ 开局

    async def open_game(self, guild_id: int, channel_id: int, user_id: int, event_id: str,
                        source_message_id: int | None) -> tuple[dict, bool]:
        """按门票事件开一局。返回 (局, 是否新建)；同一 EVENT_ID 再来只返回已有的局。"""
        async with self.store.transaction(guild_id) as doc:
            if event_id in doc["events"]:
                return doc["games"][doc["events"][event_id]], False
            doc["seq"]["game"] += 1
            gid = str(doc["seq"]["game"])
            game = {
                "id": gid, "event_id": event_id, "user_id": str(user_id), "channel_id": str(channel_id),
                "source_message_id": str(source_message_id) if source_message_id else None,
                "panel_channel_id": None, "panel_message_id": None,
                "status": PENDING, "created_at": _iso(self.now()),
                "candidates": [engine.roll_candidate(self.cfg, self.rng)
                               for _ in range(self.cfg["candidates_per_game"])],
                "result": None,
            }
            doc["games"][gid] = game
            doc["events"][event_id] = gid
            return game, True

    async def set_panel(self, guild_id: int, game_id: str, channel_id: int, message_id: int) -> None:
        async with self.store.transaction(guild_id) as doc:
            game = doc["games"].get(str(game_id))
            if game is not None:
                game["panel_channel_id"], game["panel_message_id"] = str(channel_id), str(message_id)

    def game(self, guild_id: int, game_id: str) -> dict | None:
        return self.store.load(guild_id)["games"].get(str(game_id))

    def own_game(self, guild_id: int, game_id: str, user_id: int) -> dict:
        game = self.game(guild_id, game_id)
        if game is None:
            raise ClawError("找不到这局娃娃机。")
        if game["user_id"] != str(user_id):
            raise ClawError("这不是你的娃娃机哦，用自己的抽奖券来玩吧～")
        return game

    def pending_games(self, guild_id: int, user_id: int) -> list[dict]:
        games = self.store.load(guild_id)["games"].values()
        return sorted((g for g in games if g["user_id"] == str(user_id) and g["status"] == PENDING),
                      key=lambda g: int(g["id"]))

    def check_choice(self, game: dict, idx: int, technique: str | None = None) -> None:
        if not 0 <= idx < len(game["candidates"]):
            raise ClawError("没有这只娃娃。")
        if technique is not None and technique not in self.cfg["techniques"]:
            raise ClawError("没有这种抓法。")

    # ------------------------------------------------------------------ 抓取

    async def grab(self, guild_id: int, game_id: str, user_id: int, idx: int,
                   technique: str) -> tuple[dict, dict | None, bool]:
        """一局唯一一次抓取判定。返回 (局, 娃娃或 None, 是否只是重放已有结果)。娃娃落盘后才返回成功。"""
        async with self.store.transaction(guild_id) as doc:
            game = doc["games"].get(str(game_id))
            if game is None:
                raise ClawError("找不到这局娃娃机。")
            if game["user_id"] != str(user_id):
                raise ClawError("这不是你的娃娃机哦，用自己的抽奖券来玩吧～")
            if game["result"] is not None:
                did = game["result"].get("doll_id")
                return game, doc["dolls"].get(did) if did else None, True
            self.check_choice(game, idx, technique)
            cand = game["candidates"][idx]
            p = engine.chance(self.cfg, cand["grip"], cand["position"], technique)
            success = self.rng.randrange(100) < p
            now = _iso(self.now())
            result = {"pick": idx, "technique": technique, "chance": p, "success": success, "at": now}
            st = doc["stats"].setdefault(str(user_id), _new_stats())
            st["plays"] += 1
            doll = None
            if success:
                doll = self._new_doll(doc, game, cand, user_id, now)
                result["doll_id"] = doll["id"]
                st["wins"] += 1
                key = engine.combo_key(cand)
                if key not in st["combos"]:
                    st["combos"].append(key)
                if doll["egg"]:
                    result.update(self._record_discovery(doc, doll, user_id, now))
            else:
                result["fail_text"] = self.rng.choice(self.cfg["fail_texts"])
            game["result"] = result
            game["status"] = DONE
            return game, doll, False

    @staticmethod
    def _record_discovery(doc: dict, doll: dict, user_id: int, now: str) -> dict:
        """与写入娃娃在同一事务里：个人首次发现（每人每种一条）+ 全服首发现（每种一条，先落盘者得）。"""
        eid, flags = doll["egg"]["id"], {}
        mine = doc["egg_discoveries"].setdefault(str(user_id), {})
        if eid not in mine:
            mine[eid] = {"doll_id": doll["id"], "at": now}
            flags["new_discovery"] = True
        if eid not in doc["first_discoveries"]:
            doc["first_discoveries"][eid] = {
                "egg_id": eid, "guild_id": doc["guild_id"], "user_id": str(user_id), "doll_id": doll["id"],
                "discovered_at": now, "announcement_status": "pending",     # pending / sent / failed / skipped
                "announcement_message_id": None, "announcement_attempts": 0, "announcement_error": None}
            flags["server_first"] = True
        return flags

    def _new_doll(self, doc: dict, game: dict, cand: dict, user_id: int, now: str) -> dict:
        doc["seq"]["doll"] += 1
        did = f"{self.cfg['doll_id_prefix']}{doc['seq']['doll']:06d}"
        egg = engine.match_egg(self.cfg, cand)
        doll = {
            "id": did, **{f: cand[f] for f in engine.FIELDS}, "names": dict(cand["names"]),
            "name": engine.doll_name(cand["names"]),
            "egg": {"id": egg["id"], "title": egg["title"], "story": egg.get("story", ""),
                    "reveal_text": egg["reveal_text"], "emoji": egg["emoji"]} if egg else None,
            "original_owner_id": str(user_id), "current_owner_id": str(user_id),
            "caught_at": now, "acquired_at": now, "is_showcased": False,
            "source_game_id": game["id"], "pending_gift": None, "history": [],
        }
        doc["dolls"][did] = doll
        return doll

    # ------------------------------------------------------------------ 收藏

    def doll(self, guild_id: int, doll_id: str) -> dict | None:
        return self.store.load(guild_id)["dolls"].get(doll_id)

    def collection(self, guild_id: int, user_id: int, flt: str = "all") -> list[dict]:
        """当前持有的娃娃，最新获得的在前。"""
        dolls = [d for d in self.store.load(guild_id)["dolls"].values() if d["current_owner_id"] == str(user_id)]
        if flt == "eggs":
            dolls = [d for d in dolls if d["egg"]]
        elif flt == "showcase":
            dolls = [d for d in dolls if d["is_showcased"]]
        return sorted(dolls, key=lambda d: (d["acquired_at"], d["id"]), reverse=True)

    def page(self, guild_id: int, user_id: int, flt: str, page: int) -> tuple[list[dict], int, int]:
        """返回 (本页娃娃, 规范化后的页码, 总页数)。"""
        items = self.collection(guild_id, user_id, flt)
        size = self.cfg["page_size"]
        pages = max(1, -(-len(items) // size))
        page = max(0, min(page, pages - 1))
        return items[page * size:(page + 1) * size], page, pages

    def showcase(self, guild_id: int, user_id: int) -> list[dict]:
        return sorted(self.collection(guild_id, user_id, "showcase"), key=lambda d: d["id"])

    async def set_showcase(self, guild_id: int, user_id: int, doll_id: str, on: bool) -> dict:
        async with self.store.transaction(guild_id) as doc:
            doll = self._owned(doc, doll_id, user_id)
            if on and not doll["is_showcased"]:
                count = sum(1 for d in doc["dolls"].values()
                            if d["current_owner_id"] == str(user_id) and d["is_showcased"])
                if count >= self.cfg["max_showcase"]:
                    raise ClawError(f"展示位最多 {self.cfg['max_showcase']} 只，先取消一只再来。")
            doll["is_showcased"] = on
            return doll

    @staticmethod
    def _owned(doc: dict, doll_id: str, user_id: int) -> dict:
        doll = doc["dolls"].get(doll_id)
        if doll is None:
            raise ClawError("找不到这只娃娃。")
        if doll["current_owner_id"] != str(user_id):
            raise ClawError("这只娃娃现在不在你手里。")
        return doll

    # ------------------------------------------------------------------ 统计

    def stats(self, guild_id: int, user_id: int) -> dict:
        doc = self.store.load(guild_id)
        st = doc["stats"].get(str(user_id), _new_stats())
        mine = [d for d in doc["dolls"].values() if d["current_owner_id"] == str(user_id)]
        return {
            "plays": st["plays"], "wins": st["wins"],
            "holding": len(mine),
            "holding_caught": sum(1 for d in mine if d["original_owner_id"] == str(user_id)),
            "holding_gifted": sum(1 for d in mine if d["original_owner_id"] != str(user_id)),
            "unique_combos": len(st["combos"]), "eggs": len(doc["egg_discoveries"].get(str(user_id), {})),
            "eggs_total": len(self.cfg["_eggs"]),
            "gifts_sent": st["gifts_sent"], "gifts_received": st["gifts_received"],
        }

    # ------------------------------------------------------------------ 彩蛋图鉴与首发现

    def discoveries(self, guild_id: int, user_id: int) -> dict:
        """玩家亲手发现过的彩蛋：彩蛋 id -> {doll_id, at}。收到的赠礼不算。"""
        return self.store.load(guild_id)["egg_discoveries"].get(str(user_id), {})

    def first_discoveries(self, guild_id: int) -> dict:
        return self.store.load(guild_id)["first_discoveries"]

    def pending_announcements(self, guild_id: int) -> list[dict]:
        firsts = self.first_discoveries(guild_id).values()
        return sorted((r for r in firsts if r["announcement_status"] == "pending"), key=lambda r: r["discovered_at"])

    async def begin_announcement(self, guild_id: int, egg_id: str) -> dict:
        """发之前先记一次尝试：之后再重试时，attempts > 0 就先去频道里查是不是其实已经发出去了。"""
        async with self.store.transaction(guild_id) as doc:
            rec = doc["first_discoveries"][egg_id]
            rec["announcement_attempts"] = rec.get("announcement_attempts", 0) + 1
            return dict(rec)

    async def finish_announcement(self, guild_id: int, egg_id: str, message_id: int | None,
                                  error: str | None = None) -> None:
        async with self.store.transaction(guild_id) as doc:
            rec = doc["first_discoveries"][egg_id]
            if rec["announcement_status"] != "pending":
                return
            if message_id is not None:
                rec["announcement_status"], rec["announcement_message_id"] = "sent", str(message_id)
                rec["announcement_error"] = None
            else:
                rec["announcement_error"] = (error or "")[:200]
                if rec["announcement_attempts"] >= self.cfg["announce_max_attempts"]:
                    rec["announcement_status"] = "failed"

    # ------------------------------------------------------------------ 赠送

    def _expire_in(self, doc: dict, gift: dict) -> bool:
        if gift["status"] == GIFT_PENDING and _parse(gift["expires_at"]) <= self.now():
            gift["status"] = GIFT_EXPIRED
            gift["closed_at"] = _iso(self.now())
            doll = doc["dolls"].get(gift["doll_id"])
            if doll and doll["pending_gift"] == gift["id"]:
                doll["pending_gift"] = None
            return True
        return False

    async def create_gift(self, guild_id: int, user_id: int, doll_id: str, to_id: int,
                          to_is_bot: bool = False) -> dict:
        async with self.store.transaction(guild_id) as doc:
            doll = self._owned(doc, doll_id, user_id)
            if int(to_id) == int(user_id):
                raise ClawError("不能送给自己哦。")
            if to_is_bot:
                raise ClawError("不能送给 Bot。")
            if doll["pending_gift"]:
                old = doc["gifts"].get(doll["pending_gift"])
                if old is not None and not self._expire_in(doc, old) and old["status"] == GIFT_PENDING:
                    raise ClawError("这只娃娃已经有一份待对方确认的赠礼了。")
                doll["pending_gift"] = None
            doc["seq"]["gift"] += 1
            gid = str(doc["seq"]["gift"])
            now = self.now()
            gift = {"id": gid, "doll_id": doll_id, "from_id": str(user_id), "to_id": str(to_id),
                    "status": GIFT_PENDING, "created_at": _iso(now),
                    "expires_at": _iso(now + timedelta(hours=self.cfg["gift_expire_hours"])),
                    "channel_id": None, "message_id": None, "closed_at": None}
            doc["gifts"][gid] = gift
            doll["pending_gift"] = gid
            return gift

    async def cancel_gift_draft(self, guild_id: int, gift_id: str) -> None:
        """邀请消息发不出去时撤回赠礼并解锁娃娃。"""
        async with self.store.transaction(guild_id) as doc:
            gift = doc["gifts"].get(str(gift_id))
            if gift and gift["status"] == GIFT_PENDING:
                gift["status"], gift["closed_at"] = GIFT_CANCELLED, _iso(self.now())
                doll = doc["dolls"].get(gift["doll_id"])
                if doll and doll["pending_gift"] == gift["id"]:
                    doll["pending_gift"] = None

    async def set_gift_message(self, guild_id: int, gift_id: str, channel_id: int, message_id: int) -> None:
        async with self.store.transaction(guild_id) as doc:
            gift = doc["gifts"].get(str(gift_id))
            if gift is not None:
                gift["channel_id"], gift["message_id"] = str(channel_id), str(message_id)

    def gift(self, guild_id: int, gift_id: str) -> dict | None:
        return self.store.load(guild_id)["gifts"].get(str(gift_id))

    async def respond_gift(self, guild_id: int, gift_id: str, user_id: int, op: str) -> tuple[dict, dict, bool]:
        """op: accept / decline（接收者）、cancel（赠送者）。返回 (赠礼, 娃娃, 本次是否改变了状态)。"""
        async with self.store.transaction(guild_id) as doc:
            gift = doc["gifts"].get(str(gift_id))
            if gift is None:
                raise ClawError("找不到这份赠礼。")
            doll = doc["dolls"][gift["doll_id"]]
            if op in ("accept", "decline") and gift["to_id"] != str(user_id):
                raise ClawError("这份赠礼不是送给你的。")
            if op == "cancel" and gift["from_id"] != str(user_id):
                raise ClawError("只有赠送人可以撤回。")
            if self._expire_in(doc, gift):
                return gift, doll, True
            if gift["status"] != GIFT_PENDING:
                return gift, doll, False
            now = _iso(self.now())
            if op == "accept":
                if doll["current_owner_id"] != gift["from_id"]:          # 理论上被锁住不会发生，防御一下
                    gift["status"], gift["closed_at"] = GIFT_CANCELLED, now
                    raise ClawError("这只娃娃已经不在赠送人手里了。")
                doll["current_owner_id"] = gift["to_id"]
                doll["acquired_at"] = now
                doll["is_showcased"] = False
                doll["history"].append({"from": gift["from_id"], "to": gift["to_id"], "at": now, "gift_id": gift["id"]})
                doc["stats"].setdefault(gift["from_id"], _new_stats())["gifts_sent"] += 1
                doc["stats"].setdefault(gift["to_id"], _new_stats())["gifts_received"] += 1
                gift["status"] = GIFT_ACCEPTED
            else:
                gift["status"] = GIFT_DECLINED if op == "decline" else GIFT_CANCELLED
            gift["closed_at"] = now
            if doll["pending_gift"] == gift["id"]:
                doll["pending_gift"] = None
            return gift, doll, True

    async def expire_gifts(self, guild_id: int) -> list[dict]:
        """把已过期的待处理赠礼关掉并解锁娃娃，返回本次过期的赠礼（给 cog 去改邀请消息）。"""
        doc = self.store.load(guild_id)
        if not any(g["status"] == GIFT_PENDING and _parse(g["expires_at"]) <= self.now()
                   for g in doc["gifts"].values()):
            return []
        async with self.store.transaction(guild_id) as doc:
            return [dict(g) for g in doc["gifts"].values() if self._expire_in(doc, g)]

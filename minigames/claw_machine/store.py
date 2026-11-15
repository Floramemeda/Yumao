"""万象娃娃机：每 Guild 一个 JSON（门票事件、游戏局、娃娃、赠礼、统计）。

与偷糖 / 皮套相同的持久化方式：每 Guild 一把锁，事务内改副本，fsync + 原子替换成功后才替换内存；
事务块抛异常则整笔丢弃。损坏文件抛 StorageError，绝不当空文件覆盖。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class StorageError(Exception):
    pass


def empty_doc(guild_id: int) -> dict:
    return {
        "schema_version": SCHEMA_VERSION, "guild_id": str(guild_id),
        "seq": {"game": 0, "doll": 0, "gift": 0},
        "events": {},      # EVENT_ID -> 游戏局 id（唯一约束：一个门票事件只对应一局）
        "games": {},       # 游戏局 id -> 局
        "dolls": {},       # 娃娃编号 -> 娃娃
        "gifts": {},       # 赠礼 id -> 赠礼
        "stats": {},       # 玩家 id -> 统计
        # 三个不同的概念：娃娃身上的 egg（抓到了什么）、egg_discoveries（谁亲手发现过哪种）、
        # first_discoveries（每种彩蛋的全服首发现者，一种只有一条，写入后永不改）
        "egg_discoveries": {},    # 玩家 id -> 彩蛋 id -> {doll_id, at}
        "first_discoveries": {},  # 彩蛋 id -> {user_id, doll_id, discovered_at, announcement_*}
    }


def backfill_discoveries(doc: dict) -> None:
    """旧存档没有发现记录时，按娃娃的原始抓获者和抓获时间补出来。补出来的首发现不再补发公告。"""
    mine, first = doc["egg_discoveries"], doc["first_discoveries"]
    for d in sorted(doc["dolls"].values(), key=lambda d: (d["caught_at"], d["id"])):
        if not d.get("egg"):
            continue
        eid, uid = d["egg"]["id"], d["original_owner_id"]
        mine.setdefault(uid, {}).setdefault(eid, {"doll_id": d["id"], "at": d["caught_at"]})
        first.setdefault(eid, {"egg_id": eid, "guild_id": doc["guild_id"], "user_id": uid, "doll_id": d["id"],
                               "discovered_at": d["caught_at"], "announcement_status": "skipped",
                               "announcement_message_id": None, "announcement_attempts": 0})


class ClawStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.docs: dict[int, dict] = {}
        self.locks: dict[int, asyncio.Lock] = {}

    def lock(self, guild_id: int) -> asyncio.Lock:
        return self.locks.setdefault(guild_id, asyncio.Lock())

    def path(self, guild_id: int) -> Path:
        return self.data_dir / f"{int(guild_id)}.json"

    def guild_ids_on_disk(self) -> list[int]:
        if not self.data_dir.exists():
            return []
        return [int(p.stem) for p in self.data_dir.glob("*.json") if p.stem.isdigit()]

    def load(self, guild_id: int) -> dict:
        """只读视图（带缓存）。调用方不能修改返回值。"""
        if guild_id in self.docs:
            return self.docs[guild_id]
        p = self.path(guild_id)
        if not p.exists():
            doc = empty_doc(guild_id)
        else:
            try:
                with open(p, encoding="utf-8") as f:
                    doc = json.load(f)
                if doc.get("guild_id") != str(guild_id):
                    raise ValueError("guild_id 不匹配")
                legacy = "egg_discoveries" not in doc
                for key, default in empty_doc(guild_id).items():
                    doc.setdefault(key, default)
                if legacy:
                    backfill_discoveries(doc)
            except (OSError, ValueError) as e:
                log.error("万象娃娃机：guild=%s 存档无法读取（%s），已停止该服读写，请人工检查 %s",
                          guild_id, type(e).__name__, p)
                raise StorageError(str(e)) from e
        self.docs[guild_id] = doc
        return doc

    def _persist(self, guild_id: int, doc: dict) -> None:
        p = self.path(guild_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except OSError as e:
            log.error("万象娃娃机：guild=%s 写盘失败（%s）", guild_id, type(e).__name__)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StorageError(str(e)) from e

    @asynccontextmanager
    async def transaction(self, guild_id: int):
        """持锁获得副本；块正常结束则原子落盘并替换内存，抛异常则丢弃修改。"""
        async with self.lock(guild_id):
            doc = copy.deepcopy(self.load(guild_id))
            yield doc
            self._persist(guild_id, doc)
            self.docs[guild_id] = doc

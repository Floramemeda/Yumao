"""RP 骰点与场次：每 Guild 一个 JSON（自动骰点频道 + 场次 + 审计事件）。

与皮套系统相同的持久化方式：每 Guild 一把锁，事务内改副本，fsync + 原子替换成功后才替换内存；
损坏文件抛 StorageError，绝不当空文件覆盖。仅保证单进程一致。
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


def _empty(guild_id: int) -> dict:
    return {"schema_version": SCHEMA_VERSION, "guild_id": str(guild_id), "roll_channels": {}, "sessions": {}}


class RpStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.docs: dict[int, dict] = {}
        self.locks: dict[int, asyncio.Lock] = {}

    def lock(self, guild_id: int) -> asyncio.Lock:
        return self.locks.setdefault(guild_id, asyncio.Lock())

    def path(self, guild_id: int) -> Path:
        return self.data_dir / f"{int(guild_id)}.json"

    def load(self, guild_id: int) -> dict:
        """只读视图（带缓存）。调用方不能修改返回值。"""
        if guild_id in self.docs:
            return self.docs[guild_id]
        p = self.path(guild_id)
        if not p.exists():
            doc = _empty(guild_id)
        else:
            try:
                with open(p, encoding="utf-8") as f:
                    doc = json.load(f)
                if doc.get("guild_id") != str(guild_id):
                    raise ValueError("guild_id 不匹配")
                for key, default in _empty(guild_id).items():
                    doc.setdefault(key, default)
            except (OSError, ValueError) as e:
                log.error("RP：guild=%s 存档无法读取（%s），已停止该服 RP 读写，请人工检查 %s",
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
            log.error("RP：guild=%s 写盘失败（%s）", guild_id, type(e).__name__)
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

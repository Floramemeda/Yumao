"""皮套系统：每 Guild 一个 JSON（配额、Profile、草稿、Webhook id）+ 审计 JSONL。

- 每 Guild 一把 asyncio.Lock；所有修改在 transaction() 里对副本进行，持久化成功才替换内存；
- 写临时文件 + fsync + os.replace；文件损坏抛 StorageError，绝不当空文件覆盖；
- 审计按行追加（fsync），Profile 删除不影响审计归属；
- 不保存 Webhook token，日志里不打印草稿正文。
- 仅保证单进程一致：多实例部署必须改用共享锁或数据库。
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
    return {"schema_version": SCHEMA_VERSION, "guild_id": str(guild_id), "grants": {}, "profiles": [],
            "drafts": {}, "webhook_ids": {}}


class PersonaStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.docs: dict[int, dict] = {}
        self.locks: dict[int, asyncio.Lock] = {}

    def lock(self, guild_id: int) -> asyncio.Lock:
        return self.locks.setdefault(guild_id, asyncio.Lock())

    def path(self, guild_id: int) -> Path:
        return self.data_dir / f"{int(guild_id)}.json"

    def audit_path(self, guild_id: int) -> Path:
        return self.data_dir / "audit" / f"{int(guild_id)}.jsonl"

    # ------------------------------------------------------------------ 读取

    def load(self, guild_id: int) -> dict:
        """读取（带缓存）。返回的是内部文档，调用方只能读不能改。"""
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
                log.error("皮套：guild=%s 存档无法读取（%s），已停止该服皮套读写，请人工检查 %s",
                          guild_id, type(e).__name__, p)
                raise StorageError(str(e)) from e
        self.docs[guild_id] = doc
        return doc

    def all_webhook_ids(self) -> set[int]:
        """启动时扫描所有 Guild 文件里记录的皮套 Webhook id（损坏文件跳过）。"""
        ids: set[int] = set()
        if not self.data_dir.exists():
            return ids
        for p in self.data_dir.glob("*.json"):
            try:
                with open(p, encoding="utf-8") as f:
                    ids.update(int(v) for v in json.load(f).get("webhook_ids", {}).values())
            except (OSError, ValueError):
                continue
        return ids

    # ------------------------------------------------------------------ 写入

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
            log.error("皮套：guild=%s 写盘失败（%s）", guild_id, type(e).__name__)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StorageError(str(e)) from e

    @asynccontextmanager
    async def transaction(self, guild_id: int):
        """持锁获得文档副本；块正常结束则原子落盘并替换内存，抛异常则丢弃修改。"""
        async with self.lock(guild_id):
            doc = copy.deepcopy(self.load(guild_id))
            yield doc
            self._persist(guild_id, doc)
            self.docs[guild_id] = doc

    def append_audit(self, guild_id: int, record: dict) -> None:
        """审计按行追加；失败只记日志，不影响已经发出的消息。"""
        p = self.audit_path(guild_id)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            log.error("皮套：guild=%s 审计写入失败（%s）", guild_id, type(e).__name__)

    def find_audit(self, guild_id: int, message_id: int) -> dict | None:
        p = self.audit_path(guild_id)
        if not p.exists():
            return None
        target = str(message_id)
        found = None
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if target in rec.get("message_ids", []):
                    found = rec
        return found

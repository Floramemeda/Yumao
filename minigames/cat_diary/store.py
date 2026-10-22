"""喵咪日事本：每 Guild 一个 JSON 文件的存储、冷却与检索。不依赖 Discord。

- 每 Guild 一把 asyncio.Lock，冷却检查与写入在锁内 check-and-set；
- 新状态先在副本上构造，写临时文件 + fsync + os.replace 成功后才替换内存，写盘失败不留半截状态；
- 文件损坏时抛 StorageError，绝不当成空文件覆盖；
- 只能在单进程内保证互斥：多个 Bot 实例不能共用同一个数据目录。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..common import PROJECT_ROOT
from .text import search_key

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# 写入结果
OK = "ok"
DUPLICATE = "duplicate"
COOLDOWN = "cooldown"
FULL = "full"
STORAGE_ERROR = "storage_error"


class StorageError(Exception):
    pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def remaining_seconds(until: datetime, now: datetime) -> int:
    return max(1, math.ceil((until - now).total_seconds()))


@dataclass
class WriteResult:
    status: str
    retry_after: int = 0


@dataclass
class ReadResult:
    status: str                 # OK / COOLDOWN / STORAGE_ERROR
    entries: list[dict] | None = None
    retry_after: int = 0


class DiaryStore:
    def __init__(self, cfg: dict, clock=None, data_dir: Path | None = None):
        self.cfg = cfg
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.data_dir = data_dir or PROJECT_ROOT / cfg["data_dir"]
        self.docs: dict[int, dict] = {}            # guild_id -> 已加载的文档
        self.keys: dict[int, dict[str, str]] = {}  # guild_id -> {entry id: 检索键}（仅内存缓存）
        self.locks: dict[int, asyncio.Lock] = {}

    def lock(self, guild_id: int) -> asyncio.Lock:
        return self.locks.setdefault(guild_id, asyncio.Lock())

    def path(self, guild_id: int) -> Path:
        return self.data_dir / f"{int(guild_id)}.json"

    # ------------------------------------------------------------------ 读写文件

    def _load(self, guild_id: int) -> dict:
        if guild_id in self.docs:
            return self.docs[guild_id]
        p = self.path(guild_id)
        if not p.exists():
            doc = {"schema_version": SCHEMA_VERSION, "guild_id": str(guild_id), "entries": [],
                   "cooldowns": {"write": {}, "read": {}}}
        else:
            try:
                with open(p, encoding="utf-8") as f:
                    doc = json.load(f)
                if doc.get("guild_id") != str(guild_id) or not isinstance(doc.get("entries"), list):
                    raise ValueError("guild_id 不匹配或 entries 缺失")
            except (OSError, ValueError) as e:
                # 不记录正文，只记 guild 与错误类型；不覆盖原文件
                log.error("喵咪日事本：guild=%s 存档无法读取（%s），已停止该服读写，请人工检查 %s",
                          guild_id, type(e).__name__, p)
                raise StorageError(str(e)) from e
            doc.setdefault("cooldowns", {"write": {}, "read": {}})
        self.docs[guild_id] = doc
        self.keys[guild_id] = {e["id"]: search_key(e["content"]) for e in doc["entries"]}
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
            log.error("喵咪日事本：guild=%s 写盘失败（%s）", guild_id, type(e).__name__)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StorageError(str(e)) from e

    @staticmethod
    def _next_version(doc: dict) -> dict:
        """新状态副本：条目只追加不修改，浅拷贝即可；冷却会被修改，需深拷贝。"""
        new = dict(doc)
        new["cooldowns"] = copy.deepcopy(doc["cooldowns"])
        return new

    @staticmethod
    def _prune_cooldowns(doc: dict, now: datetime) -> None:
        for kind in ("write", "read"):
            doc["cooldowns"][kind] = {u: t for u, t in doc["cooldowns"].get(kind, {}).items() if _parse(t) > now}

    # ------------------------------------------------------------------ 写入

    async def add(self, guild_id: int, user_id: int, channel_id: int, message_id: int,
                  content: str, created_at: datetime) -> WriteResult:
        async with self.lock(guild_id):
            try:
                doc = self._load(guild_id)
            except StorageError:
                return WriteResult(STORAGE_ERROR)
            now = self.clock()
            mid = str(message_id)
            if any(e["message_id"] == mid for e in doc["entries"]):
                return WriteResult(DUPLICATE)                      # 重复投递：不入库、不刷新冷却
            until = doc["cooldowns"]["write"].get(str(user_id))
            if until and _parse(until) > now:
                return WriteResult(COOLDOWN, remaining_seconds(_parse(until), now))
            if len(doc["entries"]) >= self.cfg["max_entries_per_guild"]:
                return WriteResult(FULL)

            new = self._next_version(doc)
            entry = {"id": mid, "user_id": str(user_id), "channel_id": str(channel_id), "message_id": mid,
                     "content": content, "created_at": _iso(created_at)}
            new["entries"] = doc["entries"] + [entry]
            self._prune_cooldowns(new, now)
            new["cooldowns"]["write"][str(user_id)] = _iso(now + timedelta(seconds=self.cfg["write_cooldown_seconds"]))
            try:
                self._persist(guild_id, new)                       # 笔记与冷却同一次原子提交
            except StorageError:
                return WriteResult(STORAGE_ERROR)
            self.docs[guild_id] = new
            self.keys[guild_id][entry["id"]] = search_key(content)
            return WriteResult(OK)

    # ------------------------------------------------------------------ 查询

    async def search(self, guild_id: int, caller_id: int, user_id: int | None, text: str | None) -> ReadResult:
        async with self.lock(guild_id):
            try:
                doc = self._load(guild_id)
            except StorageError:
                return ReadResult(STORAGE_ERROR)
            now = self.clock()
            until = doc["cooldowns"]["read"].get(str(caller_id))
            if until and _parse(until) > now:
                return ReadResult(COOLDOWN, retry_after=remaining_seconds(_parse(until), now))

            keys = self.keys[guild_id]
            needle = search_key(text) if text else None
            rows = [e for e in doc["entries"]
                    if (user_id is None or e["user_id"] == str(user_id))
                    and (needle is None or needle in keys[e["id"]])]
            rows.sort(key=lambda e: (e["created_at"], int(e["message_id"])), reverse=True)
            rows = rows[: self.cfg["max_results"]]

            new = self._next_version(doc)
            self._prune_cooldowns(new, now)
            new["cooldowns"]["read"][str(caller_id)] = _iso(now + timedelta(seconds=self.cfg["read_cooldown_seconds"]))
            try:
                self._persist(guild_id, new)                       # 零结果也是有效查询，同样计冷却
            except StorageError:
                return ReadResult(STORAGE_ERROR)
            self.docs[guild_id] = new
            return ReadResult(OK, entries=rows)

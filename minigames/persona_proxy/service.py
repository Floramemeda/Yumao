"""皮套系统：配额、身份组门槛、Profile 管理、草稿状态机。不依赖 Discord。

所有涉及归属的操作都同时绑定 (guild_id, owner_id)；客户端传来的 profile_id 只是查询键，不是授权证明。
"""
from __future__ import annotations

import ipaddress
import json
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ..common import PROJECT_ROOT
from .store import PersonaStore, StorageError

# 草稿状态
PENDING = "pending"
SENDING = "sending"
SENT = "sent"
FAILED = "failed"
PARTIAL = "partial_success"   # /send 第一条已发、第二条失败
UNKNOWN = "unknown"           # 请求超时等，无法确认是否发出：禁止自动重试
RETRYABLE = {PENDING, FAILED, PARTIAL}


class PersonaError(Exception):
    """可以直接展示给玩家的错误。"""


def load_config() -> dict:
    with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
        return json.load(f)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(5)}"


class PersonaService:
    def __init__(self, cfg: dict | None = None, store: PersonaStore | None = None, clock=None):
        self.cfg = cfg or load_config()
        self.store = store or PersonaStore(PROJECT_ROOT / self.cfg["data_dir"])
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ 配置与门槛

    def guild_cfg(self, guild_id: int | None) -> dict | None:
        g = self.cfg.get("guilds", {}).get(str(guild_id))
        return g if g and g.get("enabled") else None

    def has_role(self, guild_id: int, role_ids) -> bool:
        """持有任一准入身份组即通过；未配置准入身份组时 fail closed。"""
        g = self.guild_cfg(guild_id)
        allowed = {int(r) for r in (g or {}).get("allowed_role_ids", [])}
        return bool(allowed) and bool(allowed & {int(r) for r in role_ids})

    def is_unlimited(self, guild_id: int, role_ids) -> bool:
        """持有「不受配额限制」身份组的人：配额按系统上限算，/grant 对其不生效。"""
        g = self.guild_cfg(guild_id) or {}
        return bool({int(r) for r in g.get("unlimited_role_ids", [])} & {int(r) for r in role_ids})

    def limit_of(self, doc: dict, user_id: int, role_ids=()) -> int:
        if self.is_unlimited(int(doc["guild_id"]), role_ids):
            return int(self.cfg["max_profile_limit"])
        return int(doc["grants"].get(str(user_id), self.cfg["default_profile_limit"]))

    @staticmethod
    def owned(doc: dict, user_id: int) -> list[dict]:
        rows = [p for p in doc["profiles"] if p["owner_id"] == str(user_id)]
        return sorted(rows, key=lambda p: (p["created_at"], p["id"]))

    def usable(self, doc: dict, user_id: int, role_ids=()) -> list[dict]:
        """按 (created_at, id) 升序取前 limit 个；与最近使用时间无关，配额回升后自动恢复。"""
        return self.owned(doc, user_id)[: self.limit_of(doc, user_id, role_ids)]

    def require_speaker(self, guild_id: int, user_id: int, role_ids, profile_id: str) -> dict:
        """发言门槛：功能开启 + 身份组 + 配额 > 0 + Profile 属于本人且在可用前 N 个。"""
        if self.guild_cfg(guild_id) is None:
            raise PersonaError("本服没有开启皮套功能。")
        if not self.has_role(guild_id, role_ids):
            raise PersonaError("你没有使用皮套的身份组。")
        doc = self.store.load(guild_id)
        if self.limit_of(doc, user_id, role_ids) <= 0:
            raise PersonaError("你的皮套配额是 0，暂时不能使用皮套。")
        mine = {p["id"]: p for p in self.owned(doc, user_id)}
        if profile_id not in mine:
            raise PersonaError("找不到这个皮套（可能已被删除）。")
        if profile_id not in {p["id"] for p in self.usable(doc, user_id, role_ids)}:
            raise PersonaError("这个皮套超出了你当前的配额，暂时停用。")
        return dict(mine[profile_id])

    # ------------------------------------------------------------------ 校验

    def validate_name(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise PersonaError("角色名称不能为空。")
        if len(name) > self.cfg["name_max_length"]:
            raise PersonaError(f"角色名称最多 {self.cfg['name_max_length']} 个字。")
        if any(unicodedata.category(c).startswith("C") for c in name):
            raise PersonaError("角色名称不能包含换行或控制字符。")
        if any(c in name for c in "@#:`"):
            raise PersonaError("角色名称不能包含 @ # : ` 这些符号。")
        folded = unicodedata.normalize("NFKC", name).casefold()
        if folded in ("everyone", "here") or any(w.casefold() in folded for w in self.cfg["blocked_name_words"]):
            raise PersonaError("这个名称容易和官方或管理员身份混淆，请换一个。")
        return name

    def validate_avatar(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            raise PersonaError("头像 URL 不能为空。")
        if len(url) > self.cfg["avatar_url_max_length"]:
            raise PersonaError("头像 URL 太长了。")
        try:
            parts = urlsplit(url)
        except ValueError:
            raise PersonaError("头像 URL 格式不对。")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise PersonaError("头像必须是 http(s) 开头的网址。")
        if parts.username or parts.password or any(c.isspace() for c in url):
            raise PersonaError("头像 URL 格式不对。")
        host = parts.hostname.lower()
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
            raise PersonaError("头像 URL 不能指向内网地址。")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and not ip.is_global:
            raise PersonaError("头像 URL 不能指向内网地址。")
        return url

    def validate_description(self, text: str) -> str:
        text = (text or "").strip()
        if len(text) > self.cfg["description_max_length"]:
            raise PersonaError(f"人设描述最多 {self.cfg['description_max_length']} 个字。")
        return text

    def validate_message(self, text: str, limit: int | None = None) -> str:
        limit = limit or self.cfg["message_max_length"]
        if not (text or "").strip():
            raise PersonaError("发言内容不能为空。")
        if len(text) > limit:
            raise PersonaError(f"发言内容超过 {limit} 字，Discord 发不出去（草稿已保存）。")
        return text

    # ------------------------------------------------------------------ Profile 管理

    async def create_profile(self, guild_id: int, user_id: int, role_ids, name: str, avatar: str,
                             description: str) -> tuple[dict, int, int]:
        """创建并持久化；返回 (profile, 现有数量, 配额)。门槛在锁内复查。"""
        name, avatar, description = self.validate_name(name), self.validate_avatar(avatar), \
            self.validate_description(description)
        if self.guild_cfg(guild_id) is None:
            raise PersonaError("本服没有开启皮套功能。")
        if not self.has_role(guild_id, role_ids):
            raise PersonaError("你没有使用皮套的身份组。")
        async with self.store.transaction(guild_id) as doc:
            limit = self.limit_of(doc, user_id, role_ids)
            count = len(self.owned(doc, user_id))
            if limit <= 0:
                raise PersonaError("你的皮套配额是 0，不能创建皮套。")
            if count >= limit:
                raise PersonaError(f"你已经有 {count} 个皮套，达到上限 {limit}。删除一个或请管理员调整配额。")
            now = _iso(self.clock())
            profile = {"id": new_id("p"), "owner_id": str(user_id), "name": name, "avatar_url": avatar,
                       "description": description, "created_at": now, "updated_at": now, "last_used_at": None,
                       "creation_log_message_id": None, "creation_log_status": None}
            doc["profiles"].append(profile)
        return dict(profile), count + 1, limit

    def get_own(self, guild_id: int, user_id: int, profile_id: str) -> dict:
        for p in self.store.load(guild_id)["profiles"]:
            if p["id"] == profile_id and p["owner_id"] == str(user_id):
                return dict(p)
        raise PersonaError("找不到这个皮套，或它不属于你。")

    async def edit_profile(self, guild_id: int, user_id: int, profile_id: str, name: str, avatar: str,
                           description: str) -> dict:
        """本人维护自己的记录：不要求身份组/配额（但不能借此发言或新建）。"""
        name, avatar, description = self.validate_name(name), self.validate_avatar(avatar), \
            self.validate_description(description)
        async with self.store.transaction(guild_id) as doc:
            p = next((x for x in doc["profiles"] if x["id"] == profile_id and x["owner_id"] == str(user_id)), None)
            if p is None:
                raise PersonaError("找不到这个皮套，或它不属于你。")
            p.update(name=name, avatar_url=avatar, description=description, updated_at=_iso(self.clock()))
        return dict(p)

    async def delete_profile(self, guild_id: int, user_id: int, profile_id: str) -> dict:
        async with self.store.transaction(guild_id) as doc:
            p = next((x for x in doc["profiles"] if x["id"] == profile_id and x["owner_id"] == str(user_id)), None)
            if p is None:
                raise PersonaError("找不到这个皮套，或它不属于你。")
            doc["profiles"].remove(p)
        return p

    async def grant(self, guild_id: int, target_id: int, limit: int, target_role_ids=()) -> dict:
        if limit < 0 or limit > self.cfg["max_profile_limit"]:
            raise PersonaError(f"配额必须在 0–{self.cfg['max_profile_limit']} 之间。")
        async with self.store.transaction(guild_id) as doc:
            old = int(doc["grants"].get(str(target_id), self.cfg["default_profile_limit"]))
            doc["grants"][str(target_id)] = limit
            count = len(self.owned(doc, target_id))
            usable = len(self.usable(doc, target_id, target_role_ids))
        return {"old": old, "new": limit, "count": count, "usable": usable,
                "unlimited": self.is_unlimited(guild_id, target_role_ids)}

    async def mark_creation_log(self, guild_id: int, profile_id: str, status: str, message_id: int | None):
        async with self.store.transaction(guild_id) as doc:
            for p in doc["profiles"]:
                if p["id"] == profile_id:
                    p["creation_log_status"] = status
                    p["creation_log_message_id"] = str(message_id) if message_id else None

    # ------------------------------------------------------------------ 草稿

    async def save_draft(self, guild_id: int, user_id: int, profile_id: str, channel_id: int, kind: str,
                         text: str, reply_to: dict | None = None, draft_id: str | None = None,
                         extra: dict | None = None) -> dict:
        """收到正文后第一件事：持久化。同一个 draft_id 重复提交只保存一次（幂等）。"""
        draft_id = draft_id or new_id("d")
        async with self.store.transaction(guild_id) as doc:
            self._prune_drafts(doc)
            if draft_id in doc["drafts"]:
                return dict(doc["drafts"][draft_id])
            now = _iso(self.clock())
            draft = {"id": draft_id, "kind": kind, "owner_id": str(user_id), "profile_id": profile_id,
                     "channel_id": str(channel_id), "text": text, "reply_to": reply_to, "extra": extra or {},
                     "status": PENDING,
                     "message_ids": [], "sent_parts": 0, "error": None, "created_at": now, "updated_at": now}
            doc["drafts"][draft_id] = draft
        return dict(draft)

    async def claim(self, guild_id: int, user_id: int, draft_id: str) -> dict:
        """原子地把草稿从可重试状态切到 sending；并发/重复点击只有一个能拿到。"""
        async with self.store.transaction(guild_id) as doc:
            d = doc["drafts"].get(draft_id)
            if d is None or d["owner_id"] != str(user_id):
                raise PersonaError("找不到这份草稿。")
            if d["status"] == SENT:
                raise PersonaError("这份草稿已经发出去了。")
            if d["status"] == SENDING:
                raise PersonaError("这份草稿正在发送中。")
            if d["status"] == UNKNOWN:
                raise PersonaError("这份草稿上次发送结果不明，为避免重复发言不会自动重试，请先确认频道里是否已经发出。")
            d["status"] = SENDING
            d["updated_at"] = _iso(self.clock())
        return dict(d)

    async def finish(self, guild_id: int, draft_id: str, status: str, message_ids=(), sent_parts: int | None = None,
                     error: str | None = None, profile_id: str | None = None) -> dict:
        async with self.store.transaction(guild_id) as doc:
            d = doc["drafts"][draft_id]
            d["status"] = status
            d["message_ids"] = d["message_ids"] + [str(m) for m in message_ids]
            if sent_parts is not None:
                d["sent_parts"] = sent_parts
            d["error"] = error
            d["updated_at"] = _iso(self.clock())
            if status in (SENT, PARTIAL) and profile_id:
                for p in doc["profiles"]:
                    if p["id"] == profile_id:
                        p["last_used_at"] = d["updated_at"]
        return dict(d)

    def retryable(self, guild_id: int, user_id: int) -> list[dict]:
        doc = self.store.load(guild_id)
        rows = [d for d in doc["drafts"].values() if d["owner_id"] == str(user_id) and d["status"] in RETRYABLE | {UNKNOWN}]
        return sorted(rows, key=lambda d: d["created_at"], reverse=True)

    def _prune_drafts(self, doc: dict) -> None:
        cutoff = _iso(self.clock() - timedelta(days=self.cfg["draft_retention_days"]))
        doc["drafts"] = {k: d for k, d in doc["drafts"].items() if d["updated_at"] >= cutoff or d["status"] == SENDING}

    async def remember_webhook(self, guild_id: int, channel_id: int, webhook_id: int) -> None:
        async with self.store.transaction(guild_id) as doc:
            doc["webhook_ids"][str(channel_id)] = str(webhook_id)

    def audit(self, guild_id: int, record: dict) -> None:
        self.store.append_audit(guild_id, {"at": _iso(self.clock()), **record})


__all__ = ["PersonaService", "PersonaError", "StorageError", "PENDING", "SENDING", "SENT", "FAILED", "PARTIAL",
           "UNKNOWN", "RETRYABLE"]

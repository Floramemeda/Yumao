"""管理计时提醒：创建、持久化、到期前一天在通知频道 @ 设置人。

到期日 = 创建时本地日期 + days；在 (到期日 − 1 天) 的 notify_time 发提醒。
时间已过（days=1 且已过 notify_time，或 Bot 停机期间错过）则在下一次检查时补发。
先发送、成功后再标记已发送：宁可极少数情况下重复，也不漏发。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord

from ..common import PROJECT_ROOT, write_json_atomic

log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
        return json.load(f)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReminderService:
    def __init__(self, bot: discord.Client, cfg: dict | None = None, clock=None):
        self.bot = bot
        self.cfg = cfg or load_config()
        self.clock = clock or _utcnow
        self.tz = ZoneInfo(self.cfg["timezone"])
        hh, mm = self.cfg["notify_time"].split(":")
        self.notify_time = time(int(hh), int(mm))
        self.state_path = PROJECT_ROOT / self.cfg["state_file"]
        self.reminders: dict[str, dict] = {}
        self.lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------------------------ 存储

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.reminders = {r["id"]: r for r in json.load(f).get("reminders", [])}
        except (OSError, ValueError, KeyError) as e:
            backup = self.state_path.with_name(f"{self.state_path.stem}.corrupt-{int(self.clock().timestamp())}.json")
            log.error("管理提醒：读取存档失败（%s），已备份为 %s，请人工检查", e, backup)
            try:
                self.state_path.rename(backup)
            except OSError:
                log.exception("管理提醒：备份坏存档失败")
            self.reminders = {}

    def save(self) -> None:
        write_json_atomic(self.state_path, {"reminders": list(self.reminders.values())})

    # ------------------------------------------------------------------ 权限

    def is_manager(self, member) -> bool:
        return any(r.id == self.cfg["role_id"] for r in getattr(member, "roles", []))

    # ------------------------------------------------------------------ 创建

    def schedule_for(self, days: int) -> tuple[date, datetime]:
        """返回 (到期日, 提醒时间 UTC)。"""
        now = self.clock()
        due = now.astimezone(self.tz).date() + timedelta(days=days)
        notify_local = datetime.combine(due - timedelta(days=1), self.notify_time, tzinfo=self.tz)
        notify_at = max(notify_local.astimezone(timezone.utc), now)
        return due, notify_at

    async def create(self, guild_id: int, creator_id: int, item: str, days: int) -> dict:
        due, notify_at = self.schedule_for(days)
        reminder = {
            "id": uuid.uuid4().hex[:8],
            "guild_id": guild_id,
            "creator_id": creator_id,
            "item": item,
            "days": days,
            "created_at": self.clock().isoformat(),
            "due_date": due.isoformat(),
            "notify_at": notify_at.isoformat(),
            "sent_at": None,
        }
        async with self.lock:
            self._prune()
            self.reminders[reminder["id"]] = reminder
            self.save()
        return reminder

    def pending(self, guild_id: int) -> list[dict]:
        """本服尚未发送的提醒，按到期日、提醒时间排序。"""
        rows = [r for r in self.reminders.values() if r["guild_id"] == guild_id and r["sent_at"] is None]
        return sorted(rows, key=lambda r: (r["due_date"], r["notify_at"]))

    # ------------------------------------------------------------------ 发送

    def message_for(self, r: dict) -> str:
        today = self.clock().astimezone(self.tz).date()
        due = date.fromisoformat(r["due_date"])
        if today < due:
            text = f"<@{r['creator_id']}>，{r['item']}在明天到期！"
        else:  # 停机期间错过，补发时已到期
            text = f"<@{r['creator_id']}>，{r['item']}已于 {due.month} 月 {due.day} 日到期！（提醒补发）"
        return text + f"\n-# 到期日 {due.isoformat()} · /reminder 设置于 {r['created_at'][:10]}"

    async def tick(self) -> None:
        """由定时循环调用：发送所有已到提醒时间、尚未发送的提醒。"""
        now = self.clock()
        due = [r for r in self.reminders.values()
               if r["sent_at"] is None and datetime.fromisoformat(r["notify_at"]) <= now]
        if not due:
            return
        channel = await self._channel()
        if channel is None:
            log.warning("管理提醒：通知频道 %s 不可用，%d 条提醒稍后重试", self.cfg["notify_channel_id"], len(due))
            return
        for r in sorted(due, key=lambda x: x["notify_at"]):
            try:
                await channel.send(self.message_for(r), allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, replied_user=False, users=[discord.Object(r["creator_id"])]))
            except discord.HTTPException as e:
                log.warning("管理提醒：发送失败，稍后重试 id=%s: %s", r["id"], e)
                continue
            async with self.lock:
                r["sent_at"] = self.clock().isoformat()
                self._prune()
                self.save()

    def _prune(self) -> None:
        """已发送超过 30 天的提醒从存档中清理。"""
        cutoff = self.clock() - timedelta(days=30)
        self.reminders = {k: r for k, r in self.reminders.items()
                          if r["sent_at"] is None or datetime.fromisoformat(r["sent_at"]) > cutoff}

    async def _channel(self):
        cid = self.cfg["notify_channel_id"]
        channel = self.bot.get_channel(cid)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(cid)
        except discord.HTTPException as e:
            log.warning("管理提醒：无法获取通知频道 %s: %s", cid, e)
            return None

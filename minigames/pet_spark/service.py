"""撸猫续火花：进度存储、签到、22:00 催撸调度。

沿用小游戏的 JSON 存档（common.write_json_atomic）。所有读改写都在同一把锁内同步完成，
签到天然幂等；催撸先持久化 (guild, user, 本地日期) 预约记录再发送，重试/重启不会重复 @。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import discord

from ..common import PROJECT_ROOT, write_json_atomic
from . import lines as lines_mod
from . import renderer
from .progress import SPARK_MIN_STREAK, CheckinResult, Progress, checkin, current_streak, level_for, parse_levels, should_remind

log = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
BREAK_WINDOW_DAYS = 3   # 断撸只在断签后的几天内通报：刚上线时不会把历史上早就断了的人一次性全报一遍


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def load_config() -> dict:
    with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
        return json.load(f)


class PetSparkService:
    def __init__(self, bot: discord.Client, cfg: dict | None = None, lines: dict | None = None,
                 rng: random.Random | None = None, clock: Callable[[], datetime] | None = None):
        self.bot = bot
        self.cfg = cfg or load_config()
        self.lines = lines if lines is not None else lines_mod.load_lines()
        self.rng = rng or random.SystemRandom()
        self.clock = clock or _utcnow
        self.levels = parse_levels(self.cfg["levels"])
        self.state_path = PROJECT_ROOT / self.cfg["state_file"]
        hh, mm = self.cfg.get("reminder_time", "22:00").split(":")
        self.reminder_time = time(int(hh), int(mm))

        self.progress: dict[str, Progress] = {}
        # "(guild):(user):(本地日期)" -> {"status": reserved/sent/failed/skipped, "at": iso}
        self.deliveries: dict[str, dict] = {}
        # guild -> 最近一次完成催撸检查的本地日期
        self.reminder_runs: dict[str, str] = {}
        # guild -> 最近一次完成连续撸猫身份组核对的本地日期
        self.role_runs: dict[str, str] = {}
        # "(guild):(user):(最后一次撸猫的本地日期)" -> {"status": reserved/sent/skipped, "at": iso}：一次断撸只通报一次
        self.breaks: dict[str, dict] = {}
        self.lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------------------------ 存储

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            self.progress = {k: Progress.from_dict(v) for k, v in data.get("progress", {}).items()}
            self.deliveries = data.get("deliveries", {})
            self.reminder_runs = data.get("reminder_runs", {})
            self.role_runs = data.get("role_runs", {})
            self.breaks = data.get("breaks", {})
        except (OSError, ValueError, KeyError) as e:
            # 不静默丢弃玩家的长期进度：把坏存档改名备份，再以空进度启动，避免拖垮整个 Bot
            backup = self.state_path.with_name(f"{self.state_path.stem}.corrupt-{int(self.clock().timestamp())}.json")
            log.error("撸猫火花：读取存档失败（%s），已备份为 %s，请人工检查", e, backup)
            try:
                self.state_path.rename(backup)
            except OSError:
                log.exception("撸猫火花：备份坏存档失败")
            self.progress, self.deliveries, self.reminder_runs, self.role_runs, self.breaks = {}, {}, {}, {}, {}

    def save(self) -> None:
        write_json_atomic(self.state_path, {
            "progress": {k: p.to_dict() for k, p in self.progress.items()},
            "deliveries": self.deliveries,
            "reminder_runs": self.reminder_runs,
            "role_runs": self.role_runs,
            "breaks": self.breaks,
        })

    @staticmethod
    def _key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    def _get(self, guild_id: int, user_id: int) -> Progress:
        key = self._key(guild_id, user_id)
        if key not in self.progress:
            self.progress[key] = Progress(guild_id, user_id)
        return self.progress[key]

    # ------------------------------------------------------------------ 时区与配置

    def guild_cfg(self, guild_id: int) -> dict:
        return self.cfg.get("guilds", {}).get(str(guild_id), {})

    def tz(self, guild_id: int) -> ZoneInfo:
        return ZoneInfo(self.guild_cfg(guild_id).get("timezone") or self.cfg["default_timezone"])

    def local_now(self, guild_id: int) -> datetime:
        return self.clock().astimezone(self.tz(guild_id))

    def today(self, guild_id: int) -> date:
        return self.local_now(guild_id).date()

    # ------------------------------------------------------------------ 签到与查询

    async def checkin(self, guild_id: int, user_id: int) -> tuple[Progress, CheckinResult, date]:
        async with self.lock:
            today = self.today(guild_id)
            p = self._get(guild_id, user_id)
            result = checkin(p, today, self.levels, now=self.clock())
            if result.counted:
                self.save()
            return p, result, today

    def checkin_message(self, user_id: int, p: Progress, result: CheckinResult, today: date) -> str:
        return renderer.checkin_message(user_id, p, result, today, self.levels, self.lines, self.rng)

    def status(self, guild_id: int, user_id: int) -> tuple[Progress, date]:
        key = self._key(guild_id, user_id)
        p = self.progress.get(key) or Progress(guild_id, user_id)
        return p, self.today(guild_id)

    async def set_opt_out(self, guild_id: int, user_id: int, opt_out: bool) -> Progress:
        async with self.lock:
            p = self._get(guild_id, user_id)
            p.reminder_opt_out = opt_out
            p.updated_at = self.clock()
            self.save()
            return p

    # ------------------------------------------------------------------ 连续撸猫身份组

    def streak_role_guilds(self) -> list[int]:
        return [int(gid) for gid, g in self.cfg.get("guilds", {}).items() if g.get("streak_role")]

    async def _apply_streak_role(self, guild, role, p: Progress, days: int, today: date) -> None:
        """让一个人的身份组和当前连续天数一致；不在服务器（或不在缓存）的人跳过，失败只记日志。"""
        member = guild.get_member(p.user_id)
        if member is None:
            return
        qualified = current_streak(p, today) >= days
        has_role = any(r.id == role.id for r in member.roles)
        try:
            if qualified and not has_role:
                await member.add_roles(role, reason=f"撸猫火花：连续撸猫 {days} 天")
            elif has_role and not qualified:
                await member.remove_roles(role, reason="撸猫火花：断签")
        except discord.HTTPException as e:
            log.warning("撸猫火花：身份组更新失败 guild=%s user=%s: %s", guild.id, p.user_id, e)

    async def sync_member_role(self, guild_id: int, user_id: int) -> None:
        """撸猫后调用：连续天数刚好达标时立刻发身份组。"""
        cfg = self.guild_cfg(guild_id).get("streak_role")
        guild = self.bot.get_guild(guild_id)
        role = guild.get_role(int(cfg["role_id"])) if cfg and guild else None
        if role is None:
            return
        async with self.lock:
            p = self.progress.get(self._key(guild_id, user_id))
            if p is not None:
                await self._apply_streak_role(guild, role, p, int(cfg["days"]), self.today(guild_id))

    async def role_tick(self) -> None:
        """由定时循环频繁调用：每个 Guild 每个本地日核对一次，把断签的人的身份组收回（Bot 停机跨天也会补上）。

        失败的人等下一次核对或下一次撸猫时重试。"""
        for guild_id in self.streak_role_guilds():
            today = self.today(guild_id)
            if self.role_runs.get(str(guild_id)) == today.isoformat():
                continue
            try:
                await self.run_role_sync(guild_id, today)
            except Exception:
                log.exception("撸猫火花：身份组核对失败 guild=%s", guild_id)

    async def run_role_sync(self, guild_id: int, today: date) -> None:
        cfg = self.guild_cfg(guild_id)["streak_role"]
        guild = self.bot.get_guild(guild_id)
        role = guild.get_role(int(cfg["role_id"])) if guild else None
        async with self.lock:
            if role is None:
                log.warning("撸猫火花：guild=%s 找不到连续撸猫身份组，跳过今天的核对", guild_id)
            else:
                for p in [p for p in self.progress.values() if p.guild_id == guild_id]:
                    await self._apply_streak_role(guild, role, p, int(cfg["days"]), today)
            self.role_runs[str(guild_id)] = today.isoformat()
            self.save()
            await self._announce_breaks(guild_id, today)

    # ------------------------------------------------------------------ 断撸通报

    @staticmethod
    def break_message(user_id: int, streak: int, last: date) -> str:
        """固定格式，云州 Bot 按这个格式识别并扣信用分，改措辞前请先和云州那边对一下。"""
        return f"🐾 玉猫断撸：<@{user_id}> 连续 {streak} 天后中断（最后一次撸猫 {last.isoformat()}）"

    async def _announce_breaks(self, guild_id: int, today: date) -> list[int]:
        """火花点亮过（连续 ≥ 2 天）的人断签后，在 `break_channel_id` 通报一次；每次断签只一条，重启不会重复。已持有 `self.lock`。"""
        channel_id = self.guild_cfg(guild_id).get("break_channel_id")
        if not channel_id:
            return []
        channel = await self._reminder_channel(channel_id)
        guild = self.bot.get_guild(guild_id)
        if channel is None or guild is None:
            log.warning("撸猫火花：guild=%s 的断撸通报频道不可用，跳过", guild_id)
            return []
        if getattr(channel, "archived", False):                    # 通报频道是子区时，被自动归档了就先打开
            try:
                await channel.edit(archived=False)
            except discord.HTTPException as e:
                log.warning("撸猫火花：无法取消断撸通报子区的归档: %s", e)
        yesterday, earliest = today - timedelta(days=1), today - timedelta(days=BREAK_WINDOW_DAYS)
        sent: list[int] = []
        for p in sorted((p for p in self.progress.values() if p.guild_id == guild_id), key=lambda p: p.user_id):
            last = p.last_checkin_local_date
            if last is None or p.streak_count < SPARK_MIN_STREAK or last >= yesterday or last < earliest:
                continue
            key = f"{guild_id}:{p.user_id}:{last.isoformat()}"
            if key in self.breaks:
                continue
            if not await self._is_member(guild, p.user_id):
                self.breaks[key] = {"status": "skipped", "at": self.clock().isoformat()}
                continue
            self.breaks[key] = {"status": "reserved", "at": self.clock().isoformat()}      # 先落盘再发送，中途崩溃不会重复通报
            self.save()
            try:
                await channel.send(self.break_message(p.user_id, p.streak_count, last), allowed_mentions=discord.AllowedMentions.none())
                self.breaks[key]["status"] = "sent"
                sent.append(p.user_id)
            except discord.HTTPException as e:
                log.warning("撸猫火花：断撸通报发送失败 guild=%s user=%s: %s", guild_id, p.user_id, e)
                del self.breaks[key]                                                      # 下次核对再试（消息里带最后撸猫日期，对面按它去重）
            self.save()
        self._prune_breaks(today)
        return sent

    def _prune_breaks(self, today: date) -> None:
        keep_from = today - timedelta(days=30)
        self.breaks = {k: v for k, v in self.breaks.items() if date.fromisoformat(k.rsplit(":", 1)[1]) >= keep_from}

    # ------------------------------------------------------------------ 催撸

    def reminder_guilds(self) -> list[int]:
        return [int(gid) for gid, g in self.cfg.get("guilds", {}).items()
                if g.get("reminder_enabled") and g.get("reminder_channel_id")]

    async def reminder_tick(self) -> None:
        """由定时循环频繁调用：到了本地 22:00 且今天还没检查过的 Guild 执行一次催撸。

        只比较本地时间与已持久化的"今天是否已检查"，因此重启、补跑、夏令时都安全；
        22:00 后才启动的 Bot 会在当天午夜前补跑。
        """
        for guild_id in self.reminder_guilds():
            now = self.local_now(guild_id)
            today = now.date().isoformat()
            if now.time() < self.reminder_time or self.reminder_runs.get(str(guild_id)) == today:
                continue
            try:
                await self.run_reminders(guild_id, now.date())
            except Exception:
                log.exception("撸猫火花：催撸失败 guild=%s", guild_id)

    async def run_reminders(self, guild_id: int, today: date) -> list[int]:
        """执行某 Guild 当天的催撸，返回本次实际 @ 到的用户。"""
        async with self.lock:
            gcfg = self.guild_cfg(guild_id)
            channel = await self._reminder_channel(gcfg.get("reminder_channel_id"))
            guild = self.bot.get_guild(guild_id)
            self.reminder_runs[str(guild_id)] = today.isoformat()
            self._prune(today)
            if channel is None or guild is None:
                log.warning("撸猫火花：guild=%s 的催撸频道不可用，跳过今天的催撸", guild_id)
                self.save()
                return []

            candidates = [p for p in self.progress.values()
                          if p.guild_id == guild_id and should_remind(p, today)
                          and self._delivery_key(p, today) not in self.deliveries]
            targets = []
            for p in candidates:
                if await self._is_member(guild, p.user_id):
                    targets.append(p)
                else:
                    self.deliveries[self._delivery_key(p, today)] = {"status": "skipped", "at": self.clock().isoformat()}
            # 先落盘预约，再发送：发送中途崩溃也不会在重启后重复 @
            for p in targets:
                self.deliveries[self._delivery_key(p, today)] = {"status": "reserved", "at": self.clock().isoformat()}
            self.save()

            sent: list[int] = []
            for batch in self._batches(targets, today):
                ids = [p.user_id for p in batch]
                content = "\n".join(self._reminder_line(p, today) for p in batch)
                try:
                    await channel.send(content, allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=False, users=[discord.Object(i) for i in ids]))
                    status = "sent"
                    sent.extend(ids)
                except discord.HTTPException as e:
                    log.warning("撸猫火花：催撸消息发送失败 guild=%s: %s", guild_id, e)
                    status = "failed"
                for p in batch:
                    self.deliveries[self._delivery_key(p, today)] = {"status": status, "at": self.clock().isoformat()}
                self.save()
            return sent

    @staticmethod
    def _delivery_key(p: Progress, today: date) -> str:
        return f"{p.guild_id}:{p.user_id}:{today.isoformat()}"

    def _prune(self, today: date) -> None:
        keep_from = today - timedelta(days=self.cfg.get("reminder_record_keep_days", 7))
        self.deliveries = {k: v for k, v in self.deliveries.items()
                           if date.fromisoformat(k.rsplit(":", 1)[1]) >= keep_from}

    def _reminder_line(self, p: Progress, today: date) -> str:
        lv = level_for(p.streak_count, self.levels)   # 催撸对象昨天刚签过，streak_count 就是当前连续天数
        text = lines_mod.pick(self.lines, "reminder", lv.level if lv else 0, self.rng)
        mention = f"<@{p.user_id}>"
        if "{mention}" not in text:
            text = "{mention} " + text
        return lines_mod.render(text, mention=mention, streak=p.streak_count, level_name=lv.name if lv else "")

    def _batches(self, targets: list[Progress], today: date):
        """合并成少量消息：每条最多 N 人，且不超过 Discord 消息长度。"""
        per_msg = max(1, self.cfg.get("reminder_mentions_per_message", 15))
        batch, length = [], 0
        for p in targets:
            line_len = len(self._reminder_line(p, today)) + 1  # 估算，实际发送时再抽一次文案
            if batch and (len(batch) >= per_msg or length + line_len > DISCORD_MESSAGE_LIMIT - 200):
                yield batch
                batch, length = [], 0
            batch.append(p)
            length += line_len
        if batch:
            yield batch

    async def _reminder_channel(self, channel_id: int | None):
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except discord.HTTPException as e:
            log.warning("撸猫火花：无法获取催撸频道 %s: %s", channel_id, e)
            return None

    @staticmethod
    async def _is_member(guild, user_id: int) -> bool:
        if guild.get_member(user_id) is not None:
            return True
        try:
            await guild.fetch_member(user_id)
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException:
            return True  # 网络问题时不把人当成已退服

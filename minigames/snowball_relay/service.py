"""超级滚雪球：会话生命周期、回合推进、积分账本、持久化与恢复。

沿用小游戏的模式：每局一把 asyncio.Lock；所有状态存在一个 JSON 存档里（会话 + 积分账本 + 个人积分），
推动结果与终局记账在同一次原子写入中完成，账本以 (session_id, user_id) 去重。
战报先写入会话的 outbox（pending_reports）再发送，发送失败不会回滚已提交的结果。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import discord

from ..common import PROJECT_ROOT, channel_allowed, is_admin, write_json_atomic
from . import engine, renderer, views
from .models import TERMINAL, SnowballSession, Status

log = logging.getLogger(__name__)

MSG_OVER = "本局已经结束"
MSG_NOT_PARTICIPANT = "你没有参加本局"
MSG_ALREADY_JOINED = "你已经报名了"
MSG_FULL = "人数已满（最多 {n} 人）"
MSG_STARTED = "游戏已经开始，无法加入或退出"
MSG_NOT_HOST = "只有主持人可以开始游戏"
MSG_NOT_ENOUGH = "至少需要 {n} 名玩家才能开始"
MSG_NO_CANCEL = "只有主持人或管理员可以取消"
MSG_NOT_YOUR_TURN = "尚未轮到你"
MSG_ENGULFED = "你已经被卷进雪球里了，只能看大家继续推啦"
MSG_STALE = "这一棒已经结束了"
MSG_CHANNEL_BUSY = "本频道已经有一局超级滚雪球了。"
MSG_CHANNEL_NOT_ALLOWED = "这里不能开超级滚雪球哦，请到 {channels} 或其子区里开"

REPORT_MAX_ATTEMPTS = 3
REPORT_RETRY_SECONDS = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def users_only(user_ids) -> discord.AllowedMentions:
    """严格白名单：只允许提及指定用户（AllowedMentions 默认会放行 everyone/roles，必须显式关闭）。"""
    return discord.AllowedMentions(everyone=False, roles=False, replied_user=False,
                                   users=[discord.Object(u) for u in user_ids])


class SnowballService:
    def __init__(self, bot: discord.Client, cfg: dict | None = None, lines: dict | None = None,
                 seed_factory=None, clock=None):
        self.bot = bot
        self.cfg = cfg or engine.load_config()
        self.lines = lines if lines is not None else renderer.load_lines()
        self.seed_factory = seed_factory or (lambda: secrets.token_hex(8))
        self.clock = clock or _now
        self.state_path = PROJECT_ROOT / self.cfg["state_file"]

        self.sessions: dict[str, SnowballSession] = {}
        self.by_channel: dict[int, str] = {}
        self.by_message: dict[int, str] = {}
        self.ledger: dict[str, int] = {}          # "session:user" -> 入账分数（唯一）
        self.players: dict[str, dict] = {}        # "guild:user" -> 个人累计积分
        self.locks: dict[str, asyncio.Lock] = {}
        self.timers: dict[str, asyncio.Task] = {}
        self.retry_tasks: dict[str, asyncio.Task] = {}
        self._create_lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------------------------ 持久化

    def _load(self) -> None:
        self._loaded_sessions: list[SnowballSession] = []
        if not self.state_path.exists():
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            self.ledger = {k: int(v) for k, v in data.get("ledger", {}).items()}
            self.players = data.get("players", {})
            self._loaded_sessions = [SnowballSession.from_dict(d) for d in data.get("sessions", [])]
        except (OSError, ValueError, KeyError) as e:
            # 积分是长期数据：坏存档改名备份后以空状态启动，不拖垮整个 Bot，也不静默覆盖
            backup = self.state_path.with_name(f"{self.state_path.stem}.corrupt-{int(self.clock().timestamp())}.json")
            log.error("滚雪球：读取存档失败（%s），已备份为 %s，请人工检查", e, backup)
            try:
                self.state_path.rename(backup)
            except OSError:
                log.exception("滚雪球：备份坏存档失败")
            self.ledger, self.players, self._loaded_sessions = {}, {}, []

    def save(self) -> None:
        write_json_atomic(self.state_path, {
            "sessions": [s.to_dict() for s in self.sessions.values()],
            "ledger": self.ledger,
            "players": self.players,
        })

    def _lock(self, sid: str) -> asyncio.Lock:
        return self.locks.setdefault(sid, asyncio.Lock())

    def _register(self, s: SnowballSession) -> None:
        self.sessions[s.id] = s
        self.by_channel[s.channel_id] = s.id
        self.by_message[s.message_id] = s.id
        self._lock(s.id)

    def _release(self, s: SnowballSession) -> None:
        """会话结束且战报都已发出：释放频道并从存档移除（积分账本保留）。"""
        self._cancel_timer(s.id)
        self.sessions.pop(s.id, None)
        if self.by_channel.get(s.channel_id) == s.id:
            self.by_channel.pop(s.channel_id, None)
        self.by_message.pop(s.message_id, None)
        self.locks.pop(s.id, None)
        self.save()

    def _live(self, sid: str, s: SnowballSession) -> bool:
        return self.sessions.get(sid) is s and s.status not in TERMINAL

    # ------------------------------------------------------------------ 恢复

    async def restore(self) -> None:
        """重启恢复：补发未发出的战报、补完结算（幂等）、按持久化的截止时间恢复计时。"""
        await self.bot.wait_until_ready()
        for s in self._loaded_sessions:
            self._register(s)
            async with self._lock(s.id):
                if s.status == Status.SETTLING:
                    self._settle(s)          # 账本去重，重复调用不会多加分
                    self.save()
                if s.status == Status.LOBBY:
                    if await self._message_exists(s):
                        self._schedule(s)
                    else:
                        s.status = Status.CANCELLED
                        self._release(s)
                    continue
                await self._flush_reports(s)
                if s.status == Status.IN_PROGRESS:
                    await self._repost_panel(s)
                    self._schedule(s)
                elif s.status in TERMINAL and not s.pending_reports:
                    await self._edit_panel(s)
                    self._release(s)
        self._loaded_sessions = []
        self.save()

    async def _message_exists(self, s: SnowballSession) -> bool:
        channel = self.bot.get_channel(s.channel_id)
        if channel is None:
            return False
        try:
            await channel.fetch_message(s.message_id)
            return True
        except (discord.NotFound, discord.Forbidden):
            return False
        except discord.HTTPException:
            return True

    # ------------------------------------------------------------------ Discord 辅助

    @staticmethod
    async def _reply(interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.followup.send(content, ephemeral=True)
        except discord.HTTPException as e:
            log.warning("滚雪球：ephemeral 回复失败: %s", e)

    def _views_for(self, s: SnowballSession):
        if s.status == Status.LOBBY:
            return renderer.lobby_embed(s, self.cfg), views.lobby_view(s)
        if s.status == Status.CANCELLED and s.started_at is None:
            return renderer.panel_embed(s, self.cfg), views.lobby_view(s, disabled=True)
        return renderer.panel_embed(s, self.cfg), views.game_view(s, disabled=s.status != Status.IN_PROGRESS)

    async def _edit_panel(self, s: SnowballSession) -> None:
        channel = self.bot.get_channel(s.channel_id)
        if channel is None:
            return
        embed, view = self._views_for(s)
        try:
            await channel.get_partial_message(s.message_id).edit(embed=embed, view=view)
        except discord.NotFound:
            if s.status == Status.LOBBY:
                s.status = Status.CANCELLED
                self._release(s)
        except discord.HTTPException as e:
            log.warning("滚雪球：更新面板失败: %s", e)

    async def _repost_panel(self, s: SnowballSession) -> None:
        """把面板重新发到频道底部并删除旧面板（先落盘新 id，删除旧面板不会触发取消）。"""
        channel = self.bot.get_channel(s.channel_id)
        if channel is None:
            return
        embed, view = self._views_for(s)
        old_id = s.message_id
        try:
            message = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("滚雪球：重发面板失败，改为原地更新: %s", e)
            await self._edit_panel(s)
            return
        self.by_message.pop(old_id, None)
        s.message_id = message.id
        self.by_message[message.id] = s.id
        self.save()
        try:
            await channel.get_partial_message(old_id).delete()
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ 报名

    async def create_game(self, interaction: discord.Interaction) -> None:
        allowed = self.cfg.get("allowed_channel_ids") or []
        # 白名单为空 = 不限制频道
        if allowed and not channel_allowed(interaction.channel, allowed):
            channels = "、".join(f"<#{cid}>" for cid in allowed)
            await interaction.response.send_message(MSG_CHANNEL_NOT_ALLOWED.format(channels=channels), ephemeral=True)
            return
        async with self._create_lock:
            if interaction.channel_id in self.by_channel:
                await interaction.response.send_message(MSG_CHANNEL_BUSY, ephemeral=True)
                return
            now = self.clock()
            s = SnowballSession(
                id=uuid.uuid4().hex[:12], guild_id=interaction.guild_id, channel_id=interaction.channel_id,
                message_id=0, creator_user_id=interaction.user.id, status=Status.LOBBY, created_at=now,
                participants=[interaction.user.id],
                deadline=now + timedelta(seconds=self.cfg["lobby_timeout_seconds"]),
            )
            await interaction.response.send_message(embed=renderer.lobby_embed(s, self.cfg), view=views.lobby_view(s))
            message = await interaction.original_response()
            s.message_id = message.id
            self._register(s)
            self.save()
            self._schedule(s)

    async def handle_lobby(self, interaction: discord.Interaction, sid: str, op: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        s = self.sessions.get(sid)
        if s is None:
            await self._reply(interaction, MSG_OVER)
            return
        async with self._lock(sid):
            if not self._live(sid, s):
                await self._reply(interaction, MSG_OVER)
                return
            if op == "cancel":
                await self._cancel_locked(s, interaction)
                return
            if s.status != Status.LOBBY:
                await self._reply(interaction, MSG_STARTED)
                return
            uid = interaction.user.id
            if op == "join":
                if uid in s.participants:
                    return await self._reply(interaction, MSG_ALREADY_JOINED)
                if len(s.participants) >= self.cfg["max_players"]:
                    return await self._reply(interaction, MSG_FULL.format(n=self.cfg["max_players"]))
                s.participants.append(uid)
                self.save()
                await self._reply(interaction, "报名成功！")
                await self._edit_panel(s)
            elif op == "leave":
                if uid not in s.participants:
                    return await self._reply(interaction, MSG_NOT_PARTICIPANT)
                s.participants.remove(uid)
                if not s.participants:
                    await self._cancel_locked(s, interaction, reason="所有人都退出了")
                    return
                if uid == s.creator_user_id:
                    s.creator_user_id = s.participants[0]   # 主持人转移给最早报名的人
                self.save()
                await self._reply(interaction, "已退出报名。")
                await self._edit_panel(s)
            elif op == "start":
                if uid != s.creator_user_id:
                    return await self._reply(interaction, MSG_NOT_HOST)
                if len(s.participants) < self.cfg["min_players"]:
                    return await self._reply(interaction, MSG_NOT_ENOUGH.format(n=self.cfg["min_players"]))
                engine.start(s, self.seed_factory(), self.cfg)
                s.status = Status.IN_PROGRESS
                s.started_at = self.clock()
                s.deadline = self.clock() + timedelta(seconds=self.cfg["turn_timeout_seconds"])
                s.version += 1
                self.save()
                await self._reply(interaction, "开始滚雪球！")
                await self._send(s, content="⛄ 超级滚雪球开始！接力顺序：" + " → ".join(f"<@{u}>" for u in s.relay_order)
                                 + f"\n第一棒：<@{s.current_actor_id}>",
                                 allowed_mentions=users_only(s.participants))
                await self._repost_panel(s)
                self._schedule(s)

    async def handle_cancel(self, interaction: discord.Interaction, sid: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        s = self.sessions.get(sid)
        if s is None:
            return await self._reply(interaction, MSG_OVER)
        async with self._lock(sid):
            if not self._live(sid, s):
                return await self._reply(interaction, MSG_OVER)
            await self._cancel_locked(s, interaction)

    async def _cancel_locked(self, s: SnowballSession, interaction: discord.Interaction, reason: str | None = None):
        if reason is None and interaction.user.id != s.creator_user_id and not is_admin(interaction):
            await self._reply(interaction, MSG_NO_CANCEL)
            return
        s.status = Status.CANCELLED
        s.ended_at = self.clock()
        s.version += 1
        self._release(s)   # 取消不计分
        await self._reply(interaction, "已取消本局，不计分。")
        await self._edit_panel(s)
        await self._send(s, content=f"⛄ 本局超级滚雪球已被 <@{interaction.user.id}> 取消"
                                    + (f"（{reason}）" if reason else "") + "，不计分。")

    async def _send(self, s: SnowballSession, **kwargs) -> bool:
        channel = self.bot.get_channel(s.channel_id)
        if channel is None:
            return False
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        try:
            await channel.send(**kwargs)
            return True
        except discord.HTTPException as e:
            log.warning("滚雪球：发送消息失败: %s", e)
            return False

    # ------------------------------------------------------------------ 推动

    async def handle_action(self, interaction: discord.Interaction, sid: str, seq: int, action: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        s = self.sessions.get(sid)
        if s is None:
            return await self._reply(interaction, MSG_OVER)
        async with self._lock(sid):
            if not self._live(sid, s) or s.status != Status.IN_PROGRESS:
                return await self._reply(interaction, MSG_OVER)
            uid = interaction.user.id
            if uid not in s.participants:
                return await self._reply(interaction, MSG_NOT_PARTICIPANT)
            if uid in s.engulfed_user_ids:
                return await self._reply(interaction, MSG_ENGULFED)
            if seq != s.action_sequence:
                return await self._reply(interaction, MSG_STALE)
            if uid != s.current_actor_id:
                return await self._reply(interaction, MSG_NOT_YOUR_TURN)
            await self._reply(interaction, f"你选择了「{renderer.ACTION_LABELS[action]}」！")
            await self._push_locked(s, action, auto=False)

    async def _push_locked(self, s: SnowballSession, action: str, auto: bool) -> None:
        """执行一棒（调用方持有锁）。推动结果、终局记账、战报 outbox 在同一次落盘中提交。"""
        entry = engine.push(s, action, self.cfg, auto=auto)
        s.version += 1
        s.pending_reports.append({"type": "push", "seq": entry["seq"], "attempts": 0})
        if entry["finished"]:
            s.status = Status.SETTLING
            s.ended_at = self.clock()
            self._settle(s)
            s.pending_reports.append({"type": "final", "attempts": 0})
            s.deadline = None
        else:
            s.deadline = self.clock() + timedelta(seconds=self.cfg["turn_timeout_seconds"])
        self.save()

        await self._flush_reports(s)
        if s.status == Status.IN_PROGRESS:
            await self._repost_panel(s)
            self._schedule(s)
        else:
            self._cancel_timer(s.id)
            await self._edit_panel(s)
            if not s.pending_reports:
                self._release(s)

    def _settle(self, s: SnowballSession) -> None:
        """终局记账（幂等）：每位冻结参赛者获得完整团队分；账本键 (session, user) 唯一。"""
        b = engine.score_breakdown(s, self.cfg)
        s.final_team_score = b["total"]
        s.score_breakdown = b
        awards = {}
        for uid in s.participants:
            key = f"{s.id}:{uid}"
            if key in self.ledger:
                awards[str(uid)] = self.ledger[key]
                continue
            self.ledger[key] = b["total"]
            p = self.players.setdefault(f"{s.guild_id}:{uid}", {
                "guild_id": s.guild_id, "user_id": uid, "total_score": 0, "games_played": 0, "last_score": 0})
            p["total_score"] += b["total"]
            p["games_played"] += 1
            p["last_score"] = b["total"]
            awards[str(uid)] = b["total"]
        s.awards = awards
        s.status = Status.COMPLETED
        s.settled_at = s.settled_at or self.clock()

    async def _flush_reports(self, s: SnowballSession) -> None:
        """按顺序发出 outbox 里的战报；失败的保留并稍后重试，超过次数则放弃（不影响已提交结果）。"""
        while s.pending_reports:
            report = s.pending_reports[0]
            if report["type"] == "push":
                entry = next(e for e in s.action_log if e["seq"] == report["seq"])
                embed = renderer.report_embed(entry, self.cfg, self.lines, f"{s.rng_seed}:{entry['seq']}:flavor")
                content = f"<@{entry['next_actor']}>" if entry["next_actor"] else None
                ok = await self._send(s, content=content, embed=embed,
                                      allowed_mentions=users_only([entry["next_actor"]] if entry["next_actor"] else []))
            else:
                embed = renderer.final_embed(s, self.cfg, self.lines, self.leaderboard(s.guild_id))
                ok = await self._send(s, content=" ".join(f"<@{u}>" for u in s.participants), embed=embed,
                                      allowed_mentions=users_only(s.participants))
            if not ok:
                report["attempts"] += 1
                if report["attempts"] >= REPORT_MAX_ATTEMPTS:
                    log.error("滚雪球：战报多次发送失败，放弃 session=%s report=%s", s.id, report)
                    s.pending_reports.pop(0)
                    self.save()
                    continue
                self.save()
                self._schedule_retry(s)
                return
            s.pending_reports.pop(0)
            self.save()

    def _schedule_retry(self, s: SnowballSession) -> None:
        if s.id in self.retry_tasks and not self.retry_tasks[s.id].done():
            return

        async def retry():
            await asyncio.sleep(REPORT_RETRY_SECONDS)
            async with self._lock(s.id):
                if self.sessions.get(s.id) is not s:
                    return
                await self._flush_reports(s)
                if s.status in TERMINAL and not s.pending_reports:
                    self._release(s)
        self.retry_tasks[s.id] = asyncio.create_task(retry())

    # ------------------------------------------------------------------ 计时

    def _schedule(self, s: SnowballSession) -> None:
        self._cancel_timer(s.id)
        if s.deadline is None:
            return
        self.timers[s.id] = asyncio.create_task(self._timeout(s.id, s.status, s.action_sequence, s.deadline))

    def _cancel_timer(self, sid: str) -> None:
        task = self.timers.pop(sid, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _timeout(self, sid: str, phase: Status, seq: int, deadline: datetime) -> None:
        try:
            await asyncio.sleep(max(0.0, (deadline - self.clock()).total_seconds()))
            s = self.sessions.get(sid)
            if s is None:
                return
            async with self._lock(sid):
                if not self._live(sid, s) or s.status != phase:
                    return
                if phase == Status.LOBBY:
                    s.status = Status.CANCELLED
                    s.ended_at = self.clock()
                    self._release(s)
                    await self._edit_panel(s)
                    await self._send(s, content="⛄ 报名超时，本局超级滚雪球已取消。")
                elif phase == Status.IN_PROGRESS and s.action_sequence == seq:
                    # 超时托管：只执行一次（棒次已变化则什么都不做）
                    await self._push_locked(s, self.cfg["timeout_action"], auto=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("滚雪球：计时器处理失败 session=%s", sid)

    # ------------------------------------------------------------------ 排行榜与清理

    def leaderboard(self, guild_id: int, limit: int = 10) -> list[dict]:
        rows = [p for p in self.players.values() if int(p["guild_id"]) == guild_id]
        rows.sort(key=lambda p: (-p["total_score"], -p["games_played"], p["user_id"]))
        return rows[:limit]

    async def on_message_deleted(self, message_id: int) -> None:
        sid = self.by_message.get(message_id)
        s = self.sessions.get(sid) if sid else None
        if s is None:
            return
        async with self._lock(sid):
            if not self._live(sid, s):
                return
            if s.status == Status.LOBBY:
                s.status = Status.CANCELLED
                self._release(s)
                await self._send(s, content="⛄ 报名面板被删除了，本局已取消。")
            elif s.status == Status.IN_PROGRESS:
                await self._repost_panel(s)   # 进行中的面板被删：重新发一个，不中断游戏

    def shutdown(self) -> None:
        for task in list(self.timers.values()) + list(self.retry_tasks.values()):
            task.cancel()
        self.timers.clear()
        self.retry_tasks.clear()

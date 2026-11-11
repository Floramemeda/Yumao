"""沙滩排球：会话生命周期、阶段推进、持久化与恢复。

沿用小游戏的模式：每局一把 asyncio.Lock；所有进行中的局存在一个 JSON 存档里。
战报先写入会话的 outbox（pending_reports）再发送，发送失败不会回滚已提交的结果。
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import discord

from ..common import PROJECT_ROOT, category_of, is_admin, write_json_atomic
from . import engine, renderer, views
from .models import (DIRECTION_LABELS, DIRECTIONS, PART_LABELS, PHASE_RECEIVE, PHASE_SERVE, TERMINAL, Status,
                     VolleyballSession)

log = logging.getLogger(__name__)

MSG_OVER = "本局已经结束"
MSG_NOT_PARTICIPANT = "你没有参加本局"
MSG_ALREADY_JOINED = "你已经报名了"
MSG_FULL = "人数已满（最多 {n} 人）"
MSG_STARTED = "游戏已经开始，无法加入或退出"
MSG_NOT_HOST = "只有主持人可以开始游戏"
MSG_NOT_ENOUGH = "至少需要 {n} 名玩家才能开始"
MSG_NO_CANCEL = "只有主持人或管理员可以取消"
MSG_NOT_SERVER = "现在不是你发球"
MSG_NOT_RECEIVER = "这一球不是你接"
MSG_OUT = "你已经失能退场了，在场边看着吧"
MSG_CHOSEN = "你这一球已经选过了"
MSG_STALE = "这一球已经结束了"
MSG_CHANNEL_BUSY = "本频道已经有一局沙滩排球了。"
MSG_CHANNEL_NOT_ALLOWED = "这里不能开沙滩排球哦。"

REPORT_MAX_ATTEMPTS = 3
REPORT_RETRY_SECONDS = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def users_only(user_ids) -> discord.AllowedMentions:
    """严格白名单：只允许提及指定用户。"""
    return discord.AllowedMentions(everyone=False, roles=False, replied_user=False,
                                   users=[discord.Object(u) for u in user_ids])


class VolleyballService:
    def __init__(self, bot: discord.Client, cfg: dict | None = None, seed_factory=None, clock=None):
        self.bot = bot
        self.cfg = cfg or engine.load_config()
        self.seed_factory = seed_factory or (lambda: secrets.token_hex(8))
        self.clock = clock or _now
        self.state_path = PROJECT_ROOT / self.cfg["state_file"]

        self.sessions: dict[str, VolleyballSession] = {}
        self.by_channel: dict[int, str] = {}
        self.by_message: dict[int, str] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.timers: dict[str, asyncio.Task] = {}
        self.retry_tasks: dict[str, asyncio.Task] = {}
        self._create_lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------------------------ 持久化

    def _load(self) -> None:
        self._loaded_sessions: list[VolleyballSession] = []
        if not self.state_path.exists():
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            self._loaded_sessions = [VolleyballSession.from_dict(d) for d in data.get("sessions", [])]
        except (OSError, ValueError, KeyError) as e:
            backup = self.state_path.with_name(f"{self.state_path.stem}.corrupt-{int(self.clock().timestamp())}.json")
            log.error("沙滩排球：读取存档失败（%s），已备份为 %s，请人工检查", e, backup)
            try:
                self.state_path.rename(backup)
            except OSError:
                log.exception("沙滩排球：备份坏存档失败")
            self._loaded_sessions = []

    def save(self) -> None:
        write_json_atomic(self.state_path, {"sessions": [s.to_dict() for s in self.sessions.values()]})

    def _lock(self, sid: str) -> asyncio.Lock:
        return self.locks.setdefault(sid, asyncio.Lock())

    def _register(self, s: VolleyballSession) -> None:
        self.sessions[s.id] = s
        self.by_channel[s.channel_id] = s.id
        self.by_message[s.message_id] = s.id
        self._lock(s.id)

    def _release(self, s: VolleyballSession) -> None:
        """会话结束且战报都已发出：释放频道并从存档移除。"""
        self._cancel_timer(s.id)
        self.sessions.pop(s.id, None)
        if self.by_channel.get(s.channel_id) == s.id:
            self.by_channel.pop(s.channel_id, None)
        self.by_message.pop(s.message_id, None)
        self.locks.pop(s.id, None)
        self.save()

    def _live(self, sid: str, s: VolleyballSession) -> bool:
        return self.sessions.get(sid) is s and s.status not in TERMINAL

    # ------------------------------------------------------------------ 恢复

    async def restore(self) -> None:
        """重启恢复：补发未发出的战报，按持久化的截止时间恢复计时。"""
        await self.bot.wait_until_ready()
        for s in self._loaded_sessions:
            self._register(s)
            async with self._lock(s.id):
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

    async def _message_exists(self, s: VolleyballSession) -> bool:
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
            log.warning("沙滩排球：ephemeral 回复失败: %s", e)

    def _views_for(self, s: VolleyballSession):
        if s.status == Status.LOBBY:
            return renderer.lobby_embed(s, self.cfg), views.lobby_view(s)
        if s.started_at is None:
            return renderer.panel_embed(s, self.cfg), views.lobby_view(s, disabled=True)
        return renderer.panel_embed(s, self.cfg), views.game_view(s, disabled=s.status != Status.IN_PROGRESS)

    async def _edit_panel(self, s: VolleyballSession) -> None:
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
            log.warning("沙滩排球：更新面板失败: %s", e)

    def _to_ping(self, s: VolleyballSession) -> list[int]:
        return [s.server_id] if s.phase == PHASE_SERVE else engine.receivers(s)

    async def _repost_panel(self, s: VolleyballSession) -> None:
        """把面板重新发到频道底部并 @ 这一步要操作的人，再删除旧面板（先落盘新 id，删除旧面板不会触发取消）。"""
        channel = self.bot.get_channel(s.channel_id)
        if channel is None:
            return
        embed, view = self._views_for(s)
        old_id = s.message_id
        ping = self._to_ping(s)
        try:
            message = await channel.send(content=" ".join(f"<@{u}>" for u in ping), embed=embed, view=view,
                                         allowed_mentions=users_only(ping))
        except discord.HTTPException as e:
            log.warning("沙滩排球：重发面板失败，改为原地更新: %s", e)
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

    async def _send(self, s: VolleyballSession, **kwargs) -> bool:
        channel = self.bot.get_channel(s.channel_id)
        if channel is None:
            return False
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        try:
            await channel.send(**kwargs)
            return True
        except discord.HTTPException as e:
            log.warning("沙滩排球：发送消息失败: %s", e)
            return False

    # ------------------------------------------------------------------ 报名

    async def channel_allowed(self, channel) -> bool:
        """allowed_channel_ids / allowed_category_ids 都为空 = 不限制；否则频道（或子区的父频道）或所在分类命中其一即可。"""
        channels = self.cfg.get("allowed_channel_ids") or []
        categories = self.cfg.get("allowed_category_ids") or []
        if not channels and not categories:
            return True
        if channel.id in channels or getattr(channel, "parent_id", None) in channels:
            return True
        return bool(categories) and await category_of(channel, self.bot) in categories

    async def create_game(self, interaction: discord.Interaction, target: int | None = None) -> None:
        if not await self.channel_allowed(interaction.channel):
            await interaction.response.send_message(MSG_CHANNEL_NOT_ALLOWED, ephemeral=True)
            return
        async with self._create_lock:
            if interaction.channel_id in self.by_channel:
                await interaction.response.send_message(MSG_CHANNEL_BUSY, ephemeral=True)
                return
            now = self.clock()
            s = VolleyballSession(
                id=uuid.uuid4().hex[:12], guild_id=interaction.guild_id, channel_id=interaction.channel_id,
                message_id=0, creator_user_id=interaction.user.id, status=Status.LOBBY, created_at=now,
                target_score=target or self.cfg["default_target_score"], participants=[interaction.user.id],
                deadline=now + timedelta(seconds=self.cfg["lobby_timeout_seconds"]),
            )
            await interaction.response.send_message(embed=renderer.lobby_embed(s, self.cfg), view=views.lobby_view(s),
                                                    allowed_mentions=discord.AllowedMentions.none())
            message = await interaction.original_response()
            s.message_id = message.id
            self._register(s)
            self.save()
            self._schedule(s)

    async def handle_lobby(self, interaction: discord.Interaction, sid: str, op: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        s = self.sessions.get(sid)
        if s is None:
            return await self._reply(interaction, MSG_OVER)
        async with self._lock(sid):
            if not self._live(sid, s):
                return await self._reply(interaction, MSG_OVER)
            if op == "cancel":
                return await self._cancel_locked(s, interaction)
            if s.status != Status.LOBBY:
                return await self._reply(interaction, MSG_STARTED)
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
                    return await self._cancel_locked(s, interaction, reason="所有人都退出了")
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
                engine.start(s, self.seed_factory())
                s.status = Status.IN_PROGRESS
                s.started_at = self.clock()
                s.deadline = self.clock() + timedelta(seconds=self.cfg["serve_timeout_seconds"])
                self.save()
                await self._reply(interaction, "比赛开始！")
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

    async def _cancel_locked(self, s: VolleyballSession, interaction: discord.Interaction, reason: str | None = None):
        if reason is None and interaction.user.id != s.creator_user_id and not is_admin(interaction):
            return await self._reply(interaction, MSG_NO_CANCEL)
        s.status = Status.CANCELLED
        s.ended_at = self.clock()
        self._release(s)
        await self._reply(interaction, "已取消本局。")
        await self._edit_panel(s)
        await self._send(s, content=f"🏐 本局沙滩排球已被 <@{interaction.user.id}> 取消" + (f"（{reason}）" if reason else "") + "。")

    # ------------------------------------------------------------------ 发球与接球

    async def handle_serve(self, interaction: discord.Interaction, sid: str, seq: int, direction: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        s = self.sessions.get(sid)
        if s is None:
            return await self._reply(interaction, MSG_OVER)
        async with self._lock(sid):
            if not self._live(sid, s) or s.status != Status.IN_PROGRESS:
                return await self._reply(interaction, MSG_OVER)
            if seq != s.seq or s.phase != PHASE_SERVE:
                return await self._reply(interaction, MSG_STALE)
            if interaction.user.id != s.server_id:
                return await self._reply(interaction, MSG_NOT_SERVER)
            await self._reply(interaction, f"你把球发向了「{DIRECTION_LABELS[direction]}」，别告诉对面！")
            await self._serve_locked(s, direction, auto=False)

    async def _serve_locked(self, s: VolleyballSession, direction: str, auto: bool) -> None:
        engine.serve(s, direction, auto=auto)
        s.deadline = self.clock() + timedelta(seconds=self.cfg["receive_timeout_seconds"])
        self.save()
        await self._repost_panel(s)
        self._schedule(s)

    def _receive_error(self, s: VolleyballSession | None, sid: str, seq: int, uid: int) -> str | None:
        if s is None or not self._live(sid, s) or s.status != Status.IN_PROGRESS:
            return MSG_OVER
        if seq != s.seq or s.phase != PHASE_RECEIVE:
            return MSG_STALE
        if uid not in s.participants:
            return MSG_NOT_PARTICIPANT
        if s.players[str(uid)]["out"]:
            return MSG_OUT
        if uid not in engine.receivers(s):
            return MSG_NOT_RECEIVER
        if str(uid) in s.choices:
            return MSG_CHOSEN
        return None

    async def handle_aim(self, interaction: discord.Interaction, sid: str, seq: int, direction: str) -> None:
        """接球第一步：选好方向，回一条只有自己看得到的部位按钮（真正的校验在第二步）。"""
        s = self.sessions.get(sid)
        error = self._receive_error(s, sid, seq, interaction.user.id)
        try:
            if error:
                await interaction.response.send_message(error, ephemeral=True)
            else:
                await interaction.response.send_message(f"接{DIRECTION_LABELS[direction]}，用哪里接？",
                                                        view=views.part_view(s, direction), ephemeral=True)
        except discord.HTTPException as e:
            log.warning("沙滩排球：部位面板发送失败: %s", e)

    async def handle_receive(self, interaction: discord.Interaction, sid: str, seq: int, part: str,
                             direction: str) -> None:
        """接球第二步（在自己的部位面板上）：提交选择，并把这条面板改成结果文字。"""
        await interaction.response.defer()
        s = self.sessions.get(sid)

        async def done(text: str) -> None:
            try:
                await interaction.edit_original_response(content=text, view=None)
            except discord.HTTPException as e:
                log.warning("沙滩排球：更新部位面板失败: %s", e)

        if s is None:
            return await done(MSG_OVER)
        async with self._lock(sid):
            error = self._receive_error(s, sid, seq, interaction.user.id)
            if error:
                return await done(error)
            s.choices[str(interaction.user.id)] = [part, direction]
            self.save()
            await done(f"你准备用{PART_LABELS[part]}接{DIRECTION_LABELS[direction]}！")
            if engine.all_chosen(s):
                await self._resolve_locked(s)
            else:
                await self._edit_panel(s)

    async def _resolve_locked(self, s: VolleyballSession) -> None:
        """结算本球（调用方持有锁）。结果和战报 outbox 在同一次落盘中提交。"""
        entry = engine.resolve(s, self.cfg)
        s.pending_reports.append({"type": "round", "entry": entry, "attempts": 0})
        if entry["finished"]:
            s.status = Status.COMPLETED
            s.ended_at = self.clock()
            s.deadline = None
            s.pending_reports.append({"type": "final", "attempts": 0})
        else:
            s.deadline = self.clock() + timedelta(seconds=self.cfg["serve_timeout_seconds"])
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

    async def _flush_reports(self, s: VolleyballSession) -> None:
        """按顺序发出 outbox 里的战报；失败的保留并稍后重试，超过次数则放弃（不影响已提交结果）。"""
        while s.pending_reports:
            report = s.pending_reports[0]
            embed = renderer.report_embed(report["entry"]) if report["type"] == "round" else renderer.final_embed(s)
            if not await self._send(s, embed=embed):
                report["attempts"] += 1
                if report["attempts"] >= REPORT_MAX_ATTEMPTS:
                    log.error("沙滩排球：战报多次发送失败，放弃 session=%s type=%s", s.id, report["type"])
                    s.pending_reports.pop(0)
                    self.save()
                    continue
                self.save()
                self._schedule_retry(s)
                return
            s.pending_reports.pop(0)
            self.save()

    def _schedule_retry(self, s: VolleyballSession) -> None:
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

    def _schedule(self, s: VolleyballSession) -> None:
        self._cancel_timer(s.id)
        if s.deadline is None:
            return
        self.timers[s.id] = asyncio.create_task(self._timeout(s.id, s.status, s.seq, s.deadline))

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
                    await self._send(s, content="🏐 报名超时，本局沙滩排球已取消。")
                elif s.seq == seq and s.phase == PHASE_SERVE:
                    await self._serve_locked(s, engine.auto_direction(s, DIRECTIONS), auto=True)
                elif s.seq == seq:
                    await self._resolve_locked(s)       # 没选的人算发呆
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("沙滩排球：计时器处理失败 session=%s", sid)

    # ------------------------------------------------------------------ 清理

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
                await self._send(s, content="🏐 报名面板被删除了，本局已取消。")
            elif s.status == Status.IN_PROGRESS:
                await self._repost_panel(s)   # 进行中的面板被删：重新发一个，不中断游戏

    def shutdown(self) -> None:
        for task in list(self.timers.values()) + list(self.retry_tasks.values()):
            task.cancel()
        self.timers.clear()
        self.retry_tasks.clear()

"""媚笙阁偷薯饼：会话生命周期、回合管理、并发控制与持久化。

与《千杯不醉》相同的模式：每局一把 asyncio.Lock；结算先落盘再播报；
按 deadline 恢复计时器；面板消息被删即取消。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

import discord

from ..common import PROJECT_ROOT, channel_allowed, write_json_atomic
from . import engine, renderer, views
from .models import TERMINAL, HeistGame, Player, Status, describe

log = logging.getLogger(__name__)

MSG_GAME_OVER = "本局游戏已经结束"
MSG_NOT_PARTICIPANT = "你没有参加本局游戏"
MSG_ALREADY_JOINED = "你已经报名了"
MSG_FULL = "人数已满（最多 {n} 人）"
MSG_STARTED = "游戏已经开始，无法加入或退出"
MSG_NOT_HOST = "只有主持人可以开始游戏"
MSG_NOT_ENOUGH = "至少需要 {n} 名玩家才能开始"
MSG_NO_CANCEL = "只有主持人或管理员可以取消游戏"
MSG_NOT_STARTED = "游戏还没开始"
MSG_STALE_ROUND = "本轮已经结束，已为你刷新到当前回合。"
MSG_CHANNEL_BUSY = "本频道已经有一局偷薯饼在进行了。"
MSG_CHANNEL_NOT_ALLOWED = "这里不能开偷薯饼哦，请到 {channels} 或其子区里开"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction, "permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild or perms.manage_messages))


class HeistService:
    def __init__(self, bot: discord.Client, cfg: dict | None = None, rng: random.Random | None = None):
        self.bot = bot
        self.cfg = cfg or engine.load_config()
        self.rng = rng or random.SystemRandom()
        self.state_path = PROJECT_ROOT / self.cfg["state_file"]

        self.games: dict[str, HeistGame] = {}
        self.by_channel: dict[int, str] = {}
        self.by_message: dict[int, str] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.timers: dict[str, asyncio.Task] = {}
        self._create_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 持久化

    def save(self) -> None:
        write_json_atomic(self.state_path, {"games": [g.to_dict() for g in self.games.values()]})

    def _load(self) -> list[HeistGame]:
        if not self.state_path.exists():
            return []
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            return [HeistGame.from_dict(g) for g in data.get("games", [])]
        except (OSError, ValueError, KeyError) as e:
            log.exception("偷薯饼：读取存档失败，忽略存档: %s", e)
            return []

    def _lock(self, sid: str) -> asyncio.Lock:
        return self.locks.setdefault(sid, asyncio.Lock())

    def _register(self, game: HeistGame) -> None:
        self.games[game.session_id] = game
        self.by_channel[game.channel_id] = game.session_id
        self.by_message[game.message_id] = game.session_id
        self._lock(game.session_id)

    def _release(self, game: HeistGame) -> None:
        """进入终止状态：释放频道占用、停止计时器并从存档移除。"""
        self._cancel_timer(game.session_id)
        self.games.pop(game.session_id, None)
        if self.by_channel.get(game.channel_id) == game.session_id:
            self.by_channel.pop(game.channel_id, None)
        self.by_message.pop(game.message_id, None)
        self.locks.pop(game.session_id, None)
        self.save()

    async def restore(self) -> None:
        """重启恢复：已结算未播报的回合只补发，不重新结算；其余按 deadline 恢复计时。"""
        await self.bot.wait_until_ready()
        for game in self._load():
            if game.status in TERMINAL:
                continue
            self._register(game)
            if not await self._message_exists(game):
                log.info("偷薯饼：会话 %s 的面板消息已丢失，取消", game.session_id)
                game.status = Status.CANCELLED
                self._release(game)
                continue
            if game.status in (Status.ROUND_RESOLUTION, Status.ROUND_ANNOUNCEMENT) and game.pending_result:
                async with self._lock(game.session_id):
                    await self._publish(game)
            elif game.status in (Status.LOBBY, Status.ROUND_INPUT):
                self._schedule(game)
        self.save()

    async def _message_exists(self, game: HeistGame) -> bool:
        channel = self.bot.get_channel(game.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(game.channel_id)
            except (discord.NotFound, discord.Forbidden):
                return False
            except discord.HTTPException:
                return True
        try:
            await channel.fetch_message(game.message_id)
            return True
        except (discord.NotFound, discord.Forbidden):
            return False
        except discord.HTTPException:
            return True

    # ------------------------------------------------------------------ Discord 辅助

    async def _edit_panel(self, game: HeistGame) -> None:
        pub = engine.public_view(game)
        if game.status == Status.LOBBY:
            embed, view = renderer.lobby_embed(pub, self.cfg), views.lobby_view(game)
        elif game.status in (Status.CANCELLED, Status.EXPIRED) and game.round_no == 0:
            embed, view = renderer.panel_embed(pub, self.cfg), views.lobby_view(game, disabled=True)
        else:
            disabled = game.status in TERMINAL
            embed, view = renderer.panel_embed(pub, self.cfg), views.game_view(game, disabled=disabled)

        channel = self.bot.get_channel(game.channel_id)
        if channel is None:
            return
        try:
            await channel.get_partial_message(game.message_id).edit(embed=embed, view=view)
        except discord.NotFound:
            if game.status not in TERMINAL:
                log.info("偷薯饼：会话 %s 的面板消息已被删除，取消", game.session_id)
                game.status = Status.CANCELLED
                self._release(game)
        except discord.HTTPException as e:
            log.warning("偷薯饼：更新面板失败: %s", e)

    async def _send(self, game: HeistGame, **kwargs) -> None:
        channel = self.bot.get_channel(game.channel_id)
        if channel is None:
            return
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        try:
            await channel.send(**kwargs)
        except discord.HTTPException as e:
            log.warning("偷薯饼：发送消息失败: %s", e)

    @staticmethod
    async def _reply(interaction: discord.Interaction, content: str | None = None, **kwargs) -> None:
        try:
            await interaction.followup.send(content, ephemeral=True, **kwargs)
        except discord.HTTPException as e:
            log.warning("偷薯饼：ephemeral 回复失败: %s", e)

    def _live(self, sid: str, game: HeistGame) -> bool:
        return self.games.get(sid) is game and game.status not in TERMINAL

    # ------------------------------------------------------------------ 报名

    async def create_game(self, interaction: discord.Interaction) -> None:
        allowed = self.cfg["allowed_channel_ids"]
        if not channel_allowed(interaction.channel, allowed):
            channels = "、".join(f"<#{cid}>" for cid in allowed)
            await interaction.response.send_message(MSG_CHANNEL_NOT_ALLOWED.format(channels=channels), ephemeral=True)
            return

        async with self._create_lock:
            if interaction.channel_id in self.by_channel:
                await interaction.response.send_message(MSG_CHANNEL_BUSY, ephemeral=True)
                return
            user = interaction.user
            now = _now()
            game = HeistGame(
                session_id=uuid.uuid4().hex[:12],
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                message_id=0,
                host_id=user.id,
                status=Status.LOBBY,
                created_at=now,
                deadline=now + timedelta(seconds=self.cfg["lobby_timeout_seconds"]),
            )
            # 主持人创建即报名
            game.players[user.id] = Player(user.id, user.display_name)
            await interaction.response.send_message(
                embed=renderer.lobby_embed(engine.public_view(game), self.cfg), view=views.lobby_view(game))
            message = await interaction.original_response()
            game.message_id = message.id
            self._register(game)
            self.save()
            self._schedule(game)

    async def handle_lobby(self, interaction: discord.Interaction, sid: str, op: str) -> None:
        if op == "rules":
            await interaction.response.send_message(embed=renderer.rules_embed(), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        game = self.games.get(sid)
        if game is None:
            await self._reply(interaction, MSG_GAME_OVER)
            return

        async with self._lock(sid):
            if not self._live(sid, game):
                await self._reply(interaction, MSG_GAME_OVER)
                return
            if op == "cancel":
                await self._cancel_locked(game, interaction)
                return
            if game.status != Status.LOBBY:
                await self._reply(interaction, MSG_STARTED)
                return

            user = interaction.user
            if op == "join":
                if user.id in game.players:
                    await self._reply(interaction, MSG_ALREADY_JOINED)
                    return
                if len(game.players) >= self.cfg["max_players"]:
                    await self._reply(interaction, MSG_FULL.format(n=self.cfg["max_players"]))
                    return
                game.players[user.id] = Player(user.id, user.display_name)
                self.save()
                await self._reply(interaction, "报名成功！")
                await self._edit_panel(game)

            elif op == "leave":
                if user.id not in game.players:
                    await self._reply(interaction, MSG_NOT_PARTICIPANT)
                    return
                if user.id == game.host_id:
                    await self._cancel_locked(game, interaction, reason="主持人退出")
                    return
                del game.players[user.id]
                self.save()
                await self._reply(interaction, "已退出报名。")
                await self._edit_panel(game)

            elif op == "start":
                if user.id != game.host_id:
                    await self._reply(interaction, MSG_NOT_HOST)
                    return
                if len(game.players) < self.cfg["min_players"]:
                    await self._reply(interaction, MSG_NOT_ENOUGH.format(n=self.cfg["min_players"]))
                    return
                game.status = Status.ROLE_ASSIGNMENT
                game.started_at = _now()
                engine.assign_roles(game, self.rng)
                await self._reply(interaction, "游戏开始！点公共面板上的「我的行动」查看你的身份。")
                await self._send(game, content=(
                    f"🥔 媚笙阁偷薯饼开始！猫猫警长是 <@{game.cat.user_id}>，其余都是鼠鼠。\n"
                    + " ".join(f"<@{u}>" for u in game.players)
                    + "\n点公共面板上的「我的行动」查看身份和位置，并秘密选择行动。"),
                    allowed_mentions=discord.AllowedMentions(users=True))
                await self._begin_round(game, 1)

    async def _cancel_locked(self, game: HeistGame, interaction: discord.Interaction, reason: str | None = None):
        if reason is None and interaction.user.id != game.host_id and not is_admin(interaction):
            await self._reply(interaction, MSG_NO_CANCEL)
            return
        game.status = Status.CANCELLED
        game.finished_at = _now()
        self._release(game)
        await self._reply(interaction, "已取消本局游戏。")
        await self._edit_panel(game)
        await self._send(game, content=f"🥔 本局偷薯饼已被 <@{interaction.user.id}> 取消"
                                       + (f"（{reason}）。" if reason else "。"))

    # ------------------------------------------------------------------ 游戏中按钮

    async def handle_game_button(self, interaction: discord.Interaction, sid: str, op: str) -> None:
        if op == "rules":
            await interaction.response.send_message(embed=renderer.rules_embed(), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        game = self.games.get(sid)
        if game is None:
            await self._reply(interaction, MSG_GAME_OVER)
            return
        async with self._lock(sid):
            if not self._live(sid, game):
                await self._reply(interaction, MSG_GAME_OVER)
                return
            if op == "cancel":
                await self._cancel_locked(game, interaction)
                return
            # op == "me"：私密面板只基于点击者本人的身份生成
            if interaction.user.id not in game.players:
                await self._reply(interaction, MSG_NOT_PARTICIPANT)
                return
            if game.round_no == 0:
                await self._reply(interaction, MSG_NOT_STARTED)
                return
            embed, view = self._private_panel(game, interaction.user.id)
            await self._reply(interaction, embed=embed, view=view)

    def _private_panel(self, game: HeistGame, user_id: int, notice: str | None = None):
        pv = engine.private_view(game, user_id)
        embed = renderer.private_embed(pv, game.round_no, self.cfg["max_rounds"], notice)
        accepting = game.status == Status.ROUND_INPUT and not pv["captured"]
        view = views.private_view(game, pv["legal_actions"] if accepting else [], pv["current_action"])
        return embed, view

    async def handle_select(self, interaction: discord.Interaction, sid: str, round_no: int, value: str) -> None:
        await interaction.response.defer()
        game = self.games.get(sid)

        async def update(content=None, embed=None, view=None):
            try:
                await interaction.edit_original_response(content=content, embed=embed, view=view)
            except discord.HTTPException as e:
                log.warning("偷薯饼：更新私密面板失败: %s", e)

        if game is None:
            await update(content=MSG_GAME_OVER, embed=None, view=None)
            return
        async with self._lock(sid):
            if not self._live(sid, game):
                await update(content=MSG_GAME_OVER, embed=None, view=None)
                return
            uid = interaction.user.id
            if uid not in game.players:
                await update(content=MSG_NOT_PARTICIPANT, embed=None, view=None)
                return
            if game.status != Status.ROUND_INPUT or round_no != game.round_no:
                embed, view = self._private_panel(game, uid, MSG_STALE_ROUND)
                await update(embed=embed, view=view)
                return
            try:
                engine.submit_action(game, uid, value)
            except engine.ActionError as e:
                embed, view = self._private_panel(game, uid, f"⚠️ {e}")
                await update(embed=embed, view=view)
                return
            self.save()
            everyone = game.submitted_count() == len(game.actors())
            notice = f"✅ 已选择：{describe(value)}" + ("\n所有人都选好了，马上结算！" if everyone else "")
            embed, view = self._private_panel(game, uid, notice)
            await update(embed=embed, view=view)
            if everyone:
                await self._all_submitted_locked(game)
            else:
                await self._edit_panel(game)

    async def _all_submitted_locked(self, game: HeistGame) -> None:
        """全员选好：立即结算；配置了缓冲时间则把截止时间提前到缓冲结束（期间仍可改选）。"""
        grace = self.cfg.get("all_submitted_grace_seconds", 0)
        if grace <= 0:
            self._cancel_timer(game.session_id)
            await self._resolve_locked(game, game.round_no)
            return
        new_deadline = _now() + timedelta(seconds=grace)
        if new_deadline < game.deadline:
            game.deadline = new_deadline
            self.save()
            self._schedule(game)
        await self._edit_panel(game)

    # ------------------------------------------------------------------ 回合

    async def _begin_round(self, game: HeistGame, round_no: int) -> None:
        game.status = Status.ROUND_INPUT
        game.round_no = round_no
        game.deadline = _now() + timedelta(seconds=self.cfg["round_timeout_seconds"])
        game.pending_result = None
        self.save()
        await self._repost_panel(game)
        if game.session_id in self.games:
            self._schedule(game)

    async def _repost_panel(self, game: HeistGame) -> None:
        """每轮开始时把面板重新发到频道底部，并删除旧面板。

        先发新面板并落盘新 message_id，再删旧面板；旧 id 提前从 by_message 移除，
        这样删除旧面板触发的 on_raw_message_delete 不会误取消游戏。
        """
        channel = self.bot.get_channel(game.channel_id)
        if channel is None:
            return
        old_id = game.message_id
        try:
            message = await channel.send(embed=renderer.panel_embed(engine.public_view(game), self.cfg),
                                         view=views.game_view(game),
                                         allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("偷薯饼：重发面板失败，改为原地更新: %s", e)
            await self._edit_panel(game)
            return

        self.by_message.pop(old_id, None)
        game.message_id = message.id
        self.by_message[message.id] = game.session_id
        self.save()
        try:
            await channel.get_partial_message(old_id).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            log.warning("偷薯饼：删除旧面板失败: %s", e)

    def _schedule(self, game: HeistGame) -> None:
        self._cancel_timer(game.session_id)
        self.timers[game.session_id] = asyncio.create_task(
            self._timeout(game.session_id, game.status, game.round_no, game.deadline))

    def _cancel_timer(self, sid: str) -> None:
        task = self.timers.pop(sid, None)
        # 计时器自身触发的结算会开启下一轮并重新调度，此时不能取消自己
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _timeout(self, sid: str, phase: Status, round_no: int, deadline: datetime) -> None:
        try:
            await asyncio.sleep(max(0.0, (deadline - _now()).total_seconds()))
            game = self.games.get(sid)
            if game is None:
                return
            async with self._lock(sid):
                if phase == Status.LOBBY:
                    if self._live(sid, game) and game.status == Status.LOBBY:
                        game.status = Status.EXPIRED
                        self._release(game)
                        await self._edit_panel(game)
                else:
                    await self._resolve_locked(game, round_no)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("偷薯饼：计时器处理失败 (session=%s, round=%s)", sid, round_no)

    async def _resolve_locked(self, game: HeistGame, round_no: int) -> None:
        """结算回合（调用方必须持有锁）。同一回合只结算一次。"""
        if (not self._live(game.session_id, game)
                or game.status != Status.ROUND_INPUT
                or game.round_no != round_no
                or game.resolved_round >= round_no):
            return
        game.status = Status.ROUND_RESOLUTION
        game.pending_result = engine.resolve_round(game, self.cfg["max_rounds"])
        game.resolved_round = round_no
        # 先落盘再播报：重启后只补发，不会重新结算
        self.save()
        await self._publish(game)

    async def _publish(self, game: HeistGame) -> None:
        game.status = Status.ROUND_ANNOUNCEMENT
        result = game.pending_result
        await self._send(game, embed=renderer.round_embed(result))

        if result["winner"] is None:
            await self._begin_round(game, game.round_no + 1)
            return

        game.status = Status.FINISHED
        game.finished_at = _now()
        game.pending_result = None
        self._release(game)
        pub = engine.public_view(game)
        await self._edit_panel(game)
        content, embed = renderer.final_embed(pub, result)
        await self._send(game, content=content, embed=embed, allowed_mentions=discord.AllowedMentions(users=True))

    # ------------------------------------------------------------------ 消息丢失

    async def on_message_deleted(self, message_id: int) -> None:
        sid = self.by_message.get(message_id)
        game = self.games.get(sid) if sid else None
        if game is None:
            return
        async with self._lock(sid):
            if not self._live(sid, game):
                return
            game.status = Status.CANCELLED
            self._release(game)
            await self._send(game, content="🥔 偷薯饼的面板消息被删除了，本局已取消。")

    def shutdown(self) -> None:
        for task in self.timers.values():
            task.cancel()
        self.timers.clear()

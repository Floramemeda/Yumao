"""千杯不醉：游戏生命周期、回合管理、并发控制与持久化。"""
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
from .models import Action, DrinkingGame, GameStatus, PlayerState, PlayerStatus

log = logging.getLogger(__name__)

MSG_NOT_PARTICIPANT = "你没参加本次比赛"
MSG_ALREADY_JOINED = "你已经加入本次酒局"
MSG_ALREADY_SUBMITTED = "你已经提交了本轮行动"
MSG_OUT = "你已经退出本次比赛"
MSG_NOT_HOST = "只有房主可以开始比赛"
MSG_NOT_ENOUGH = "至少需要两名玩家才能开始"
MSG_STALE_ROUND = "本轮已经结束"
MSG_GAME_OVER = "本次比赛已经结束"
MSG_ALREADY_STARTED = "比赛已经开始，无法加入"
MSG_CHANNEL_BUSY = "本频道已经有一场未结束的酒局了，先把那场喝完吧！"
MSG_CHANNEL_NOT_ALLOWED = "这里不能开酒局哦，请到 {channels} 或其子区里开"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DrinkingService:
    def __init__(self, bot: discord.Client, cfg: dict | None = None, events: dict | None = None,
                 rng: random.Random | None = None):
        self.bot = bot
        self.cfg = cfg or engine.load_config()
        self.events = events or engine.load_events()
        self.rng = rng or random.SystemRandom()
        self.state_path = PROJECT_ROOT / self.cfg["state_file"]

        self.games: dict[str, DrinkingGame] = {}
        self.by_channel: dict[int, str] = {}
        self.by_message: dict[int, str] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.timers: dict[str, asyncio.Task] = {}
        self._create_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 持久化

    def save(self) -> None:
        write_json_atomic(self.state_path, {"games": [g.to_dict() for g in self.games.values()]})

    def _load(self) -> list[DrinkingGame]:
        if not self.state_path.exists():
            return []
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            return [DrinkingGame.from_dict(g) for g in data.get("games", [])]
        except (OSError, ValueError, KeyError) as e:
            log.exception("千杯不醉：读取存档失败，忽略存档: %s", e)
            return []

    def _register(self, game: DrinkingGame) -> None:
        self.games[game.game_id] = game
        self.by_channel[game.channel_id] = game.game_id
        self.by_message[game.message_id] = game.game_id
        self._lock(game.game_id)

    def _lock(self, game_id: str) -> asyncio.Lock:
        return self.locks.setdefault(game_id, asyncio.Lock())

    def _release(self, game: DrinkingGame) -> None:
        """游戏结束或取消：释放频道占用并从存档中移除。"""
        self._cancel_timer(game.game_id)
        self.games.pop(game.game_id, None)
        if self.by_channel.get(game.channel_id) == game.game_id:
            self.by_channel.pop(game.channel_id, None)
        self.by_message.pop(game.message_id, None)
        self.locks.pop(game.game_id, None)
        self.save()

    async def restore(self) -> None:
        """Bot 重启后恢复未结束的游戏。已完成的 RNG 不会重新执行。"""
        await self.bot.wait_until_ready()
        for game in self._load():
            if game.status in (GameStatus.FINISHED, GameStatus.CANCELLED):
                continue
            self._register(game)
            if not await self._message_exists(game):
                log.info("千杯不醉：游戏 %s 的面板消息已丢失，取消", game.game_id)
                self._release(game)
                continue
            if game.status == GameStatus.SETTLING and game.pending_result:
                async with self._lock(game.game_id):
                    await self._publish_settlement(game)
            elif game.status == GameStatus.ACTIVE:
                # 已超时的回合会在计时器中立即结算
                self._schedule_timeout(game)
        self.save()

    async def _message_exists(self, game: DrinkingGame) -> bool:
        channel = self.bot.get_channel(game.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(game.channel_id)
            except (discord.NotFound, discord.Forbidden):
                return False
            except discord.HTTPException:
                return True  # 网络问题，不贸然取消
        try:
            await channel.fetch_message(game.message_id)
            return True
        except (discord.NotFound, discord.Forbidden):
            return False
        except discord.HTTPException:
            return True

    # ------------------------------------------------------------------ Discord 辅助

    def _channel(self, game: DrinkingGame):
        return self.bot.get_channel(game.channel_id)

    async def _edit_panel(self, game: DrinkingGame) -> None:
        """刷新公共面板；消息已被删除时取消游戏。"""
        if game.status == GameStatus.LOBBY:
            embed, view = renderer.lobby_embed(game), views.lobby_view(game)
        elif game.status == GameStatus.CANCELLED:
            embed, view = renderer.panel_embed(game), views.lobby_view(game, disabled=True)
        else:
            disabled = game.status != GameStatus.ACTIVE
            embed, view = renderer.panel_embed(game), views.round_view(game, disabled=disabled)

        channel = self._channel(game)
        if channel is None:
            return
        try:
            await channel.get_partial_message(game.message_id).edit(embed=embed, view=view)
        except discord.NotFound:
            if game.status not in (GameStatus.FINISHED, GameStatus.CANCELLED):
                log.info("千杯不醉：游戏 %s 的面板消息已被删除，取消", game.game_id)
                game.status = GameStatus.CANCELLED
                self._release(game)
        except discord.HTTPException as e:
            log.warning("千杯不醉：更新面板失败: %s", e)

    async def _send(self, game: DrinkingGame, **kwargs) -> None:
        channel = self._channel(game)
        if channel is None:
            return
        try:
            await channel.send(**kwargs)
        except discord.HTTPException as e:
            log.warning("千杯不醉：发送消息失败: %s", e)

    @staticmethod
    async def _reply(interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.followup.send(content, ephemeral=True)
        except discord.HTTPException as e:
            log.warning("千杯不醉：ephemeral 回复失败: %s", e)

    # ------------------------------------------------------------------ 招募阶段

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
            game = DrinkingGame(
                game_id=uuid.uuid4().hex[:12],
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                message_id=0,
                host_id=user.id,
                status=GameStatus.LOBBY,
                round_number=0,
                created_at=_now(),
            )
            # 房主创建即加入
            game.players[user.id] = PlayerState(user.id, user.display_name)

            await interaction.response.send_message(embed=renderer.lobby_embed(game), view=views.lobby_view(game))
            message = await interaction.original_response()
            game.message_id = message.id
            self._register(game)
            self.save()

    async def handle_lobby(self, interaction: discord.Interaction, game_id: str, op: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        game = self.games.get(game_id)
        if game is None:
            await self._reply(interaction, MSG_GAME_OVER)
            return

        async with self._lock(game_id):
            if self.games.get(game_id) is not game or game.status in (GameStatus.FINISHED, GameStatus.CANCELLED):
                await self._reply(interaction, MSG_GAME_OVER)
                return
            if game.status != GameStatus.LOBBY:
                await self._reply(interaction, MSG_ALREADY_STARTED if op == "join" else "比赛已经开始")
                return

            user = interaction.user
            if op == "join":
                if user.id in game.players:
                    await self._reply(interaction, MSG_ALREADY_JOINED)
                    return
                game.players[user.id] = PlayerState(user.id, user.display_name)
                self.save()
                await self._reply(interaction, "已加入酒局！")
                await self._edit_panel(game)

            elif op == "leave":
                if user.id not in game.players:
                    await self._reply(interaction, MSG_NOT_PARTICIPANT)
                    return
                if user.id == game.host_id:
                    game.status = GameStatus.CANCELLED
                    self._release(game)
                    await self._reply(interaction, "你是房主，酒局已取消。")
                    await self._edit_panel(game)
                    await self._send(game, content=f"房主 <@{user.id}> 离席，本场酒局已取消。",
                                     allowed_mentions=discord.AllowedMentions.none())
                    return
                del game.players[user.id]
                self.save()
                await self._reply(interaction, "已退出酒局。")
                await self._edit_panel(game)

            elif op == "start":
                if user.id != game.host_id:
                    await self._reply(interaction, MSG_NOT_HOST)
                    return
                if len(game.players) < self.cfg["min_players"]:
                    await self._reply(interaction, MSG_NOT_ENOUGH)
                    return
                game.started_at = _now()
                for player in game.players.values():
                    player.drunkenness = self.cfg["initial_drunkenness"]
                    player.status = PlayerStatus.ACTIVE
                await self._reply(interaction, "比赛开始！")
                await self._begin_round(game, 1)

    # ------------------------------------------------------------------ 回合

    async def _begin_round(self, game: DrinkingGame, round_number: int) -> None:
        now = _now()
        game.status = GameStatus.ACTIVE
        game.round_number = round_number
        game.round_started_at = now
        game.round_deadline = now + timedelta(seconds=self.cfg["round_timeout_seconds"])
        game.pending_result = None
        self.save()
        await self._edit_panel(game)
        if game.game_id in self.games:
            self._schedule_timeout(game)

    async def handle_action(self, interaction: discord.Interaction, game_id: str,
                            round_number: int, action: Action) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        game = self.games.get(game_id)
        if game is None:
            await self._reply(interaction, MSG_GAME_OVER)
            return

        async with self._lock(game_id):
            if self.games.get(game_id) is not game or game.status in (GameStatus.FINISHED, GameStatus.CANCELLED):
                await self._reply(interaction, MSG_GAME_OVER)
                return
            player = game.players.get(interaction.user.id)
            if player is None:
                await self._reply(interaction, MSG_NOT_PARTICIPANT)
                return
            if player.status != PlayerStatus.ACTIVE:
                await self._reply(interaction, MSG_OUT)
                return
            if game.status != GameStatus.ACTIVE or round_number != game.round_number:
                await self._reply(interaction, MSG_STALE_ROUND)
                return
            if player.action_for(round_number) is not None:
                await self._reply(interaction, MSG_ALREADY_SUBMITTED)
                return

            player.submitted_action = action
            player.submitted_round = round_number
            self.save()
            await self._reply(interaction, renderer.action_confirm(action))

            if game.all_submitted():
                self._cancel_timer(game_id)
                await self._settle_locked(game, round_number)
            else:
                await self._edit_panel(game)

    def _schedule_timeout(self, game: DrinkingGame) -> None:
        self._cancel_timer(game.game_id)
        self.timers[game.game_id] = asyncio.create_task(
            self._timeout(game.game_id, game.round_number, game.round_deadline))

    def _cancel_timer(self, game_id: str) -> None:
        task = self.timers.pop(game_id, None)
        # 计时器自身触发的结算会开启下一轮并重新调度，此时不能取消自己
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _timeout(self, game_id: str, round_number: int, deadline: datetime) -> None:
        try:
            await asyncio.sleep(max(0.0, (deadline - _now()).total_seconds()))
            game = self.games.get(game_id)
            if game is None:
                return
            async with self._lock(game_id):
                await self._settle_locked(game, round_number)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("千杯不醉：超时结算失败 (game=%s, round=%s)", game_id, round_number)

    async def _settle_locked(self, game: DrinkingGame, round_number: int) -> None:
        """结算回合（调用方必须持有游戏锁）。同一回合只会执行一次 RNG。"""
        if (self.games.get(game.game_id) is not game
                or game.status != GameStatus.ACTIVE
                or game.round_number != round_number
                or game.settled_round >= round_number):
            return

        game.status = GameStatus.SETTLING
        game.pending_result = engine.settle_round(game, self.rng, self.cfg, self.events)
        game.settled_round = round_number
        # 先落盘再发布：重启后只补发结果，不重跑 RNG
        self.save()
        await self._publish_settlement(game)

    async def _publish_settlement(self, game: DrinkingGame) -> None:
        result = game.pending_result
        embeds = renderer.settlement_embeds(result)
        for embed in embeds:
            await self._send(game, embed=embed, allowed_mentions=discord.AllowedMentions.none())

        outcome = result["outcome"]
        if outcome["kind"] == engine.OUTCOME_CONTINUE:
            await self._begin_round(game, game.round_number + 1)
            return

        game.status = GameStatus.FINISHED
        game.finished_at = _now()
        game.pending_result = None
        self._release(game)
        await self._edit_panel(game)
        content, embed = renderer.announcement(game, result, self.events, self.rng)
        await self._send(game, content=content or None, embed=embed,
                         allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

    # ------------------------------------------------------------------ 消息丢失

    async def on_message_deleted(self, message_id: int) -> None:
        game_id = self.by_message.get(message_id)
        if game_id is None:
            return
        game = self.games.get(game_id)
        if game is None:
            return
        async with self._lock(game_id):
            if self.games.get(game_id) is not game:
                return
            game.status = GameStatus.CANCELLED
            self._release(game)
            await self._send(game, content="🍻 千杯不醉的面板消息被删除了，本场酒局已取消。")

    def shutdown(self) -> None:
        for task in self.timers.values():
            task.cancel()
        self.timers.clear()

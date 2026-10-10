"""媚笙阁偷薯饼 服务层测试（伪造 Discord 对象）：权限、泄漏、重复结算、重启恢复。"""
import asyncio
import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from minigames.potato_heist import engine
from minigames.potato_heist.models import KITCHEN, STEAL, Role, Status
from minigames.potato_heist.service import HeistService

PARENT_ID = engine.load_config()["allowed_thread_parent_ids"][0]
CHANNEL_ID = 777  # 子区 id


class FakeMessage:
    def __init__(self, mid, channel=None):
        self.id = mid
        self.channel = channel

    async def edit(self, **kwargs):
        pass

    async def delete(self):
        self.channel.removed.append(self.id)


class FakeChannel:
    def __init__(self):
        self.id = CHANNEL_ID
        self.parent_id = PARENT_ID
        self.sent = []
        self.deleted = set()   # fetch 时视为已不存在的消息
        self.removed = []      # bot 主动删除的消息
        self._next_id = 5000

    def get_partial_message(self, mid):
        return FakeMessage(mid, self)

    async def fetch_message(self, mid):
        if mid in self.deleted:
            import discord
            raise discord.NotFound(NS(status=404, reason="gone"), "gone")
        return FakeMessage(mid, self)

    async def send(self, content=None, embed=None, **kwargs):
        self.sent.append((content, embed.to_dict() if embed else None))
        self._next_id += 1
        return FakeMessage(self._next_id, self)


class FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, cid):
        return self.channel

    async def wait_until_ready(self):
        pass


class FakeInteraction:
    _next_id = 1000

    def __init__(self, channel, uid, admin=False, values=None):
        self.user = NS(id=uid, display_name=f"u{uid}")
        self.channel, self.channel_id, self.guild_id = channel, channel.id, 1
        self.permissions = NS(administrator=admin, manage_guild=False, manage_messages=False)
        self.data = {"values": values or []}
        self.replies, self.edits = [], []
        self.response = NS(defer=self._noop, send_message=self._send_message)
        self.followup = NS(send=self._followup)

    async def _noop(self, **kwargs):
        pass

    async def _send_message(self, content=None, **kwargs):
        self.replies.append(content or kwargs.get("embed"))

    async def _followup(self, content=None, ephemeral=False, embed=None, view=None):
        self.replies.append(content if content is not None else embed.to_dict())
        self.last_view = view

    async def original_response(self):
        FakeInteraction._next_id += 1
        return FakeMessage(FakeInteraction._next_id)

    async def edit_original_response(self, content=None, embed=None, view=None):
        self.edits.append(content if content is not None else embed.to_dict())


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = engine.load_config()
        self.cfg["state_file"] = str(Path(self.tmp.name) / "state.json")
        self.cfg["round_timeout_seconds"] = 3600
        self.channel = FakeChannel()
        self.svc = self._service()

    async def asyncTearDown(self):
        self.svc.shutdown()
        self.tmp.cleanup()

    def _service(self, seed=1):
        return HeistService(FakeBot(self.channel), self.cfg, rng=random.Random(seed))

    def it(self, uid, **kw):
        return FakeInteraction(self.channel, uid, **kw)

    async def lobby(self, uid, op, svc=None, **kw):
        it = self.it(uid, **kw)
        await (svc or self.svc).handle_lobby(it, self.sid, op)
        return it.replies

    async def create(self, players=3):
        await self.svc.create_game(self.it(1))
        self.sid = next(iter(self.svc.games))
        for uid in range(2, players + 1):
            await self.lobby(uid, "join")
        return self.svc.games[self.sid]

    async def started(self, players=3):
        game = await self.create(players)
        await self.lobby(1, "start")
        return game

    async def select(self, uid, value, round_no=None, svc=None):
        svc = svc or self.svc
        game = svc.games.get(self.sid)
        it = self.it(uid, values=[value])
        await svc.handle_select(it, self.sid, round_no if round_no is not None else game.round_no, value)
        return it.edits[-1]

    # ---- 报名

    async def test_only_threads_under_allowed_channel(self):
        for desc, ch in (("频道本身", NS(id=PARENT_ID)),
                         ("其他频道的子区", NS(id=888, parent_id=12345))):
            with self.subTest(desc):
                it = self.it(1)
                it.channel, it.channel_id = ch, ch.id
                await self.svc.create_game(it)
                self.assertIn("下的子区里开", it.replies[0])
                self.assertEqual(self.svc.games, {})
        await self.svc.create_game(self.it(1))  # 允许的子区
        self.assertEqual(len(self.svc.games), 1)

    async def test_player_count_bounds(self):
        game = await self.create(players=2)
        self.assertIn("至少需要 3", (await self.lobby(1, "start"))[0])
        for uid in range(3, 7):
            await self.lobby(uid, "join")
        self.assertIn("人数已满", (await self.lobby(7, "join"))[0])
        self.assertIn("只有主持人", (await self.lobby(2, "start"))[0])
        await self.lobby(1, "start")
        self.assertEqual(game.status, Status.ROUND_INPUT)
        self.assertEqual(sum(p.role == Role.CAT for p in game.players.values()), 1)
        self.assertIn("无法加入", (await self.lobby(8, "join"))[0])

    async def test_cancel_permissions(self):
        await self.create()
        self.assertIn("只有主持人或管理员", (await self.lobby(2, "cancel"))[0])
        await self.lobby(9, "cancel", admin=True)
        self.assertNotIn(self.sid, self.svc.games)
        self.assertNotIn(CHANNEL_ID, self.svc.by_channel)

    # ---- 私密面板与行动

    async def test_private_panel_is_per_user(self):
        game = await self.started()
        it = self.it(42)
        await self.svc.handle_game_button(it, self.sid, "me")
        self.assertIn("没有参加", it.replies[0])
        for uid, p in game.players.items():
            it = self.it(uid)
            await self.svc.handle_game_button(it, self.sid, "me")
            title = it.replies[0]["title"]
            self.assertIn("警长" if p.role == Role.CAT else "鼠鼠", title)
            secret_field = json.dumps(it.replies[0], ensure_ascii=False)
            self.assertEqual("密道" in secret_field, p.role == Role.MOUSE)

    async def test_select_validation(self):
        game = await self.started()
        mouse = game.mice()[0]
        # 伪造一个不合法的值（鼠在雅间，不能偷）
        self.assertIn("不可用", json.dumps(await self.select(mouse.user_id, STEAL), ensure_ascii=False))
        self.assertIsNone(mouse.action_for(1))
        # 旧回合菜单
        self.assertIn("本轮已经结束", json.dumps(await self.select(mouse.user_id, "wait", round_no=0),
                                                 ensure_ascii=False))
        # 合法选择可覆盖
        await self.select(mouse.user_id, "wait")
        await self.select(mouse.user_id, "move:corridor")
        self.assertEqual(mouse.action_for(1), "move:corridor")
        # 非参与者
        self.assertIn("没有参加", await self.select(42, "wait"))

    async def test_captured_mouse_cannot_act(self):
        game = await self.started()
        mouse = game.mice()[0]
        mouse.captured = True
        self.assertIn("被抓住", json.dumps(await self.select(mouse.user_id, "wait"), ensure_ascii=False))

    # ---- 结算

    async def test_duplicate_resolution_runs_once(self):
        game = await self.started()
        lock = self.svc._lock(self.sid)
        async with lock:
            await self.svc._resolve_locked(game, 1)
        async with self.svc._lock(self.sid):
            await self.svc._resolve_locked(game, 1)
        round_posts = [e for _, e in self.channel.sent if e and e.get("title", "").startswith("第 1 轮")]
        self.assertEqual(len(round_posts), 1)
        self.assertEqual(game.round_no, 2)

    async def test_all_timeouts_until_round_10(self):
        self.cfg["round_timeout_seconds"] = 0.01
        game = await self.started()
        # 把鼠固定在不会被默认搜查扫到的位置之外也无所谓：最终必然有胜负
        for _ in range(200):
            if self.sid not in self.svc.games:
                break
            await asyncio.sleep(0.02)
        self.assertNotIn(self.sid, self.svc.games)
        self.assertEqual(game.status, Status.FINISHED)
        self.assertEqual(game.winner, Role.CAT)
        self.assertLessEqual(game.round_no, 10)
        finals = [e for _, e in self.channel.sent if e and "获胜" in e.get("title", "")]
        self.assertEqual(len(finals), 1)

    async def test_public_posts_never_leak(self):
        from minigames.potato_heist.models import NODE_NAMES
        game = await self.started(players=4)
        for m in game.mice():
            m.position = KITCHEN
            await self.select(m.user_id, STEAL)
        self.channel.sent.clear()  # 开局消息会 @ 所有人，只检查之后的播报
        await self.select(game.cat.user_id, "up")  # 最后一人选好 → 立即结算
        self.assertEqual(game.round_no, 2)
        blob = json.dumps(self.channel.sent, ensure_ascii=False)
        self.assertIn("薯饼失窃", blob)
        self.assertNotIn(NODE_NAMES[game.secret_room], blob)
        for m in game.mice():
            self.assertNotIn(f"<@{m.user_id}>", blob)

    async def test_panel_reposted_each_round(self):
        game = await self.create()
        lobby_id = game.message_id
        await self.lobby(1, "start")
        self.assertIn(lobby_id, self.channel.removed)        # 开局后招募面板被换到底部
        round1_id = game.message_id
        self.assertNotEqual(round1_id, lobby_id)
        async with self.svc._lock(self.sid):
            await self.svc._resolve_locked(game, 1)
        self.assertEqual(game.round_no, 2)
        self.assertNotEqual(game.message_id, round1_id)
        self.assertIn(round1_id, self.channel.removed)
        # 新面板在本轮结算播报之后
        self.assertTrue(self.channel.sent[-2][1]["title"].startswith("第 1 轮"))
        self.assertTrue(self.channel.sent[-1][1]["title"].endswith("第 2 / 10 轮"))
        # 删除旧面板触发的事件不会取消游戏；删除当前面板仍会取消
        await self.svc.on_message_deleted(round1_id)
        self.assertEqual(game.status, Status.ROUND_INPUT)
        self.assertEqual(self.svc.by_message, {game.message_id: self.sid})
        saved = json.loads(Path(self.cfg["state_file"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["games"][0]["message_id"], game.message_id)
        await self.svc.on_message_deleted(game.message_id)
        self.assertEqual(game.status, Status.CANCELLED)

    async def test_resolves_as_soon_as_everyone_chose(self):
        game = await self.started(players=4)
        mice = game.mice()
        mice[0].captured = True  # 被捕的鼠不需要行动
        await self.select(mice[1].user_id, "wait")
        await self.select(game.cat.user_id, engine.legal_actions(game, game.cat.user_id)[0])
        self.assertEqual(game.round_no, 1)  # 还有人没选
        await self.select(mice[2].user_id, "move:corridor")
        await self.select(mice[2].user_id, "wait")  # 未全员前仍可改选
        self.assertEqual(game.round_no, 2)
        self.assertEqual(game.resolved_round, 1)
        self.assertEqual(game.status, Status.ROUND_INPUT)
        self.assertIn("第 1 轮", json.dumps(self.channel.sent, ensure_ascii=False))
        # 计时器已被替换为第 2 轮的；第 1 轮不会被再次结算
        await asyncio.sleep(0)
        self.assertEqual(sum("第 1 轮 · 结算" in json.dumps(e, ensure_ascii=False)
                             for _, e in self.channel.sent if e), 1)

    async def test_grace_period_shortens_deadline(self):
        self.cfg["all_submitted_grace_seconds"] = 0.05
        game = await self.started()
        for p in game.players.values():  # 猫查后厨、鼠在雅间等待：本轮无人被捕
            await self.select(p.user_id, "search:kitchen" if p.role == Role.CAT else "wait")
        self.assertEqual(game.round_no, 1)  # 缓冲期内仍可改选
        await asyncio.sleep(0.15)
        self.assertEqual(game.round_no, 2)

    # ---- 重启恢复

    async def test_restart_replays_announcement_without_reresolving(self):
        game = await self.started()
        # 模拟：结算已落盘，但播报前崩溃
        game.status = Status.ROUND_RESOLUTION
        game.pending_result = engine.resolve_round(game, self.cfg["max_rounds"])
        game.resolved_round = 1
        snapshot = json.dumps(game.to_dict(), default=str)
        self.svc.save()
        self.svc.shutdown()

        svc2 = self._service(seed=99)
        await svc2.restore()
        game2 = svc2.games[self.sid]
        self.assertEqual(game2.round_no, 2)
        self.assertEqual(game2.status, Status.ROUND_INPUT)
        before = json.loads(snapshot)
        self.assertEqual([p["position"] for p in before["players"]], [p.position for p in game2.players.values()])
        svc2.shutdown()

    async def test_restart_with_expired_deadline_resolves_once(self):
        game = await self.started()
        self.svc.shutdown()
        data = json.loads(Path(self.cfg["state_file"]).read_text(encoding="utf-8"))
        data["games"][0]["deadline"] = "2000-01-01T00:00:00+00:00"
        Path(self.cfg["state_file"]).write_text(json.dumps(data), encoding="utf-8")

        svc2 = self._service()
        await svc2.restore()
        await asyncio.sleep(0.05)
        self.assertEqual(svc2.games[self.sid].round_no, 2)
        svc2.shutdown()

    async def test_lost_panel_cancels_on_restart(self):
        game = await self.started()
        self.svc.shutdown()
        self.channel.deleted.add(game.message_id)
        svc2 = self._service()
        await svc2.restore()
        self.assertEqual(svc2.games, {})
        self.assertEqual(json.loads(Path(self.cfg["state_file"]).read_text(encoding="utf-8")), {"games": []})

    async def test_message_deleted_cancels(self):
        game = await self.started()
        await self.svc.on_message_deleted(game.message_id)
        self.assertEqual(game.status, Status.CANCELLED)
        self.assertEqual(self.svc.by_channel, {})

    async def test_lobby_expires(self):
        self.cfg["lobby_timeout_seconds"] = 0.01
        game = await self.create()
        await asyncio.sleep(0.05)
        self.assertEqual(game.status, Status.EXPIRED)
        self.assertNotIn(self.sid, self.svc.games)


if __name__ == "__main__":
    unittest.main()

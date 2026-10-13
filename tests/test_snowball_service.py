"""超级滚雪球 服务层测试（伪造 Discord 对象）：权限、并发、超时、结算幂等、重启恢复、mention 安全。"""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import discord

from minigames.snowball_relay import engine
from minigames.snowball_relay.models import Status
from minigames.snowball_relay.service import SnowballService

CFG = engine.load_config()
CHANNEL_ID = 4242  # 不限制频道：任意频道都能开局


class FakeMessage:
    def __init__(self, mid, channel):
        self.id, self.channel = mid, channel

    async def edit(self, **kwargs):
        pass

    async def delete(self):
        self.channel.removed.append(self.id)


class FakeChannel:
    def __init__(self):
        self.id = CHANNEL_ID
        self.sent = []          # (content, embed dict, allowed_mentions)
        self.removed = []
        self.fail = False
        self._next = 9000

    def get_partial_message(self, mid):
        return FakeMessage(mid, self)

    async def fetch_message(self, mid):
        return FakeMessage(mid, self)

    async def send(self, content=None, embed=None, view=None, allowed_mentions=None):
        if self.fail:
            raise discord.HTTPException(NS(status=500, reason="boom"), "boom")
        self.sent.append((content, embed.to_dict() if embed else None, allowed_mentions))
        self._next += 1
        return FakeMessage(self._next, self)


class FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, cid):
        return self.channel

    async def wait_until_ready(self):
        pass


class FakeInteraction:
    _next = 100

    def __init__(self, channel, uid, admin=False):
        self.user = NS(id=uid)
        self.channel, self.channel_id, self.guild_id = channel, channel.id, 1
        self.permissions = NS(administrator=admin, manage_guild=False, manage_messages=False)
        self.replies = []
        self.response = NS(defer=self._noop, send_message=self._send)
        self.followup = NS(send=self._followup)

    async def _noop(self, **kw):
        pass

    async def _send(self, content=None, **kw):
        self.replies.append(content)

    async def _followup(self, content=None, ephemeral=False, **kw):
        self.replies.append(content)

    async def original_response(self):
        FakeInteraction._next += 1
        return NS(id=FakeInteraction._next)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = copy.deepcopy(CFG)
        self.cfg["state_file"] = str(Path(self.tmp.name) / "state.json")
        self.cfg["turn_timeout_seconds"] = 3600
        self.channel = FakeChannel()
        self.svc = self.new_service()

    async def asyncTearDown(self):
        self.svc.shutdown()
        self.tmp.cleanup()

    def new_service(self):
        return SnowballService(FakeBot(self.channel), self.cfg, seed_factory=lambda: "fixed-seed")

    def it(self, uid, **kw):
        return FakeInteraction(self.channel, uid, **kw)

    async def lobby(self, uid, op, **kw):
        it = self.it(uid, **kw)
        await self.svc.handle_lobby(it, self.sid, op)
        return it.replies[-1]

    async def create(self, n=3):
        await self.svc.create_game(self.it(1))
        self.sid = next(iter(self.svc.sessions))
        for uid in range(2, n + 1):
            await self.lobby(uid, "join")
        return self.svc.sessions[self.sid]

    async def started(self, n=3):
        s = await self.create(n)
        await self.lobby(1, "start")
        return s

    async def act(self, uid, action="steady", seq=None, s=None):
        s = s or self.svc.sessions.get(self.sid)
        it = self.it(uid)
        await self.svc.handle_action(it, self.sid, s.action_sequence if seq is None else seq, action)
        return it.replies[-1]

    async def play_out(self, s, action="steady"):
        while s.status == Status.IN_PROGRESS:
            await self.act(s.current_actor_id, action, s=s)

    # ---- 报名

    async def test_player_bounds(self):
        s = await self.create(2)
        self.assertIn("至少需要 3", await self.lobby(1, "start"))
        for uid in range(3, 7):
            await self.lobby(uid, "join")
        self.assertIn("人数已满", await self.lobby(7, "join"))
        self.assertIn("只有主持人", await self.lobby(2, "start"))
        await self.lobby(1, "start")
        self.assertEqual(s.status, Status.IN_PROGRESS)
        self.assertEqual(sorted(s.participants), list(range(1, 7)))
        self.assertIn("无法加入", await self.lobby(8, "join"))

    async def test_host_leave_transfers_then_cancel_no_points(self):
        s = await self.create(3)
        await self.lobby(1, "leave")
        self.assertEqual(s.creator_user_id, 2)
        self.assertIn("只有主持人或管理员", await self.lobby(3, "cancel"))
        await self.lobby(2, "cancel")
        self.assertEqual(s.status, Status.CANCELLED)
        self.assertEqual((self.svc.ledger, self.svc.players), ({}, {}))

    async def test_lobby_timeout_cancels(self):
        self.cfg["lobby_timeout_seconds"] = 0.01
        s = await self.create(3)
        await asyncio.sleep(0.05)
        self.assertEqual(s.status, Status.CANCELLED)
        self.assertNotIn(CHANNEL_ID, self.svc.by_channel)

    # ---- 行动权限与并发

    async def test_only_current_actor_and_stale_buttons(self):
        s = await self.started(3)
        actor = s.current_actor_id
        other = next(u for u in s.participants if u != actor)
        self.assertIn("尚未轮到你", await self.act(other))
        self.assertIn("没有参加", await self.act(42))
        seq = s.action_sequence
        await asyncio.gather(*(self.act(actor, "hard", seq=seq, s=s) for _ in range(5)))  # 双击/并发
        self.assertEqual(s.action_sequence, seq + 1)
        self.assertIn("已经结束", await self.act(s.current_actor_id, seq=seq, s=s))      # 旧按钮
        self.assertEqual(s.action_sequence, seq + 1)

    async def test_engulfed_player_cannot_act(self):
        s = await self.started(4)
        s.engulfed_user_ids.append(s.relay_order[2])
        self.assertIn("卷进雪球", await self.act(s.relay_order[2]))

    async def test_timeout_auto_steady_once(self):
        self.cfg["turn_timeout_seconds"] = 0.02
        s = await self.started(3)
        await asyncio.sleep(0.03)
        self.svc._cancel_timer(self.sid)      # 只看第一次超时
        first = s.action_log[0]
        self.assertEqual((first["action"], first["auto"]), ("steady", True))
        self.assertEqual(s.action_sequence, 1)
        reports = [e for _, e, _ in self.channel.sent if e and e.get("title") == "第 1 棒"]
        self.assertEqual(len(reports), 1)

    # ---- 结算与积分

    async def test_full_game_awards_everyone_full_score(self):
        s = await self.started(4)
        await self.play_out(s, "aim")
        self.assertEqual(s.status, Status.COMPLETED)
        total = s.final_team_score
        self.assertEqual(total, engine.score_breakdown(s, self.cfg)["total"])
        for uid in s.participants:   # 包括被卷入的玩家
            self.assertEqual(self.svc.ledger[f"{s.id}:{uid}"], total)
            self.assertEqual(self.svc.players[f"1:{uid}"]["total_score"], total)
        self.assertNotIn(self.sid, self.svc.sessions)
        final = [(c, e, am) for c, e, am in self.channel.sent if e and "接力完成" in e.get("title", "")]
        self.assertEqual(len(final), 1)
        content, embed, am = final[0]
        self.assertFalse(am.everyone)
        self.assertEqual(sorted(u.id for u in am.users), sorted(s.participants))

    async def test_settlement_idempotent_and_restart_safe(self):
        s = await self.started(3)
        await self.play_out(s)
        snapshot = (dict(self.svc.ledger), copy.deepcopy(self.svc.players))
        self.svc._settle(s)                                   # 重复结算
        self.assertEqual((self.svc.ledger, self.svc.players), snapshot)
        svc2 = self.new_service()                             # 重启
        svc2._settle(s)
        self.assertEqual((svc2.ledger, svc2.players), snapshot)

    async def test_leaderboard_cumulative(self):
        for _ in range(2):
            s = await self.started(3)
            await self.play_out(s)
        await self.svc.create_game(self.it(4))
        self.sid = next(iter(self.svc.sessions))
        for uid in (5, 6):
            await self.lobby(uid, "join")
        await self.lobby(4, "start")
        await self.play_out(self.svc.sessions[self.sid], "repair")
        board = self.svc.leaderboard(1)
        self.assertEqual([r["user_id"] for r in board[:3]], [1, 2, 3])
        self.assertEqual(board[0]["games_played"], 2)
        scores = [r["total_score"] for r in board]
        self.assertEqual(scores, sorted(scores, reverse=True))

    async def test_publish_failure_does_not_double_score(self):
        s = await self.started(3)
        while s.remaining_turns() > 1:
            await self.act(s.current_actor_id, s=s)
        self.channel.fail = True
        await self.act(s.current_actor_id, s=s)               # 最后一棒：结算成功、播报失败
        self.assertEqual(s.status, Status.COMPLETED)
        self.assertTrue(s.pending_reports)
        ledger = dict(self.svc.ledger)
        self.channel.fail = False
        svc2 = self.new_service()                              # 重启后补发
        await svc2.restore()
        self.assertEqual(svc2.ledger, ledger)
        self.assertEqual(svc2.sessions, {})
        self.assertTrue(any(e and "接力完成" in e.get("title", "") for _, e, _ in self.channel.sent))

    # ---- 重启恢复

    async def test_restart_resumes_turn(self):
        s = await self.started(4)
        await self.act(s.current_actor_id, "hard", s=s)
        expected_actor, seq = s.current_actor_id, s.action_sequence
        self.svc.shutdown()
        svc2 = self.new_service()
        await svc2.restore()
        s2 = svc2.sessions[self.sid]
        self.assertEqual((s2.current_actor_id, s2.action_sequence), (expected_actor, seq))
        self.assertEqual(s2.action_log, s.action_log)
        svc2.shutdown()

    async def test_restart_after_deadline_auto_pushes_once(self):
        s = await self.started(3)
        self.svc.shutdown()
        data = json.loads(Path(self.cfg["state_file"]).read_text(encoding="utf-8"))
        data["sessions"][0]["deadline"] = "2000-01-01T00:00:00+00:00"
        Path(self.cfg["state_file"]).write_text(json.dumps(data), encoding="utf-8")
        svc2 = self.new_service()
        await svc2.restore()
        await asyncio.sleep(0.05)
        s2 = svc2.sessions[self.sid]
        self.assertEqual(s2.action_sequence, 1)
        self.assertTrue(s2.action_log[0]["auto"])
        svc2.shutdown()

    async def test_mentions_whitelisted(self):
        s = await self.started(3)
        await self.play_out(s, "hard")
        for content, embed, am in self.channel.sent:
            self.assertIsNotNone(am)
            self.assertFalse(am.everyone)
            self.assertFalse(am.roles)


if __name__ == "__main__":
    unittest.main()

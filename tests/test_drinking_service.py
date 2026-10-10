"""千杯不醉 服务层测试（伪造 Discord 对象）：每轮重发面板。"""
import json
import random
import tempfile
import unittest
from pathlib import Path

from minigames.drinking_contest import engine
from minigames.drinking_contest.models import Action, GameStatus
from minigames.drinking_contest.service import DrinkingService
from test_potato_service import FakeBot, FakeChannel, FakeInteraction


class DrinkingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = engine.load_config()
        self.cfg["state_file"] = str(Path(self.tmp.name) / "state.json")
        self.cfg["round_timeout_seconds"] = 3600
        self.channel = FakeChannel()
        self.channel.id = self.cfg["allowed_channel_ids"][0]
        self.svc = DrinkingService(FakeBot(self.channel), self.cfg, rng=random.Random(1))

    async def asyncTearDown(self):
        self.svc.shutdown()
        self.tmp.cleanup()

    def it(self, uid):
        return FakeInteraction(self.channel, uid)

    async def test_panel_reposted_each_round(self):
        await self.svc.create_game(self.it(1))
        gid = next(iter(self.svc.games))
        game = self.svc.games[gid]
        lobby_id = game.message_id
        await self.svc.handle_lobby(self.it(2), gid, "join")
        await self.svc.handle_lobby(self.it(1), gid, "start")

        # 开局：招募面板换成底部的新面板
        self.assertIn(lobby_id, self.channel.removed)
        round1_id = game.message_id
        self.assertNotEqual(round1_id, lobby_id)

        # 第 1 轮都喝一杯：醉酒度最多 15%，不会有人倒下，必然进入第 2 轮
        for uid in (1, 2):
            await self.svc.handle_action(self.it(uid), gid, 1, Action.DRINK)
        self.assertEqual(game.round_number, 2)
        self.assertIn(round1_id, self.channel.removed)
        self.assertNotEqual(game.message_id, round1_id)
        # 新面板在结算播报之后
        self.assertTrue(self.channel.sent[-2][1]["title"].startswith("第 1 轮"))
        self.assertIn("第 2 轮", self.channel.sent[-1][1]["title"])

        # 删除旧面板触发的事件不会取消游戏；删除当前面板仍会取消
        await self.svc.on_message_deleted(round1_id)
        self.assertEqual(game.status, GameStatus.ACTIVE)
        self.assertEqual(self.svc.by_message, {game.message_id: gid})
        saved = json.loads(Path(self.cfg["state_file"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["games"][0]["message_id"], game.message_id)
        await self.svc.on_message_deleted(game.message_id)
        self.assertEqual(game.status, GameStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()

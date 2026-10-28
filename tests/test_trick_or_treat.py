"""不给糖就捣蛋测试：python -m unittest discover tests"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import discord

from minigames.trick_or_treat import renderer as ui
from minigames.trick_or_treat.cog import TrickOrTreat
from minigames.trick_or_treat.constants import ACTIVE, BODY_PARTS, CLOSED, FOUND, JOINED, PART_IDS
from minigames.trick_or_treat.service import TotError, TotService, load_config, matched_ids, stats
from minigames.trick_or_treat.store import StorageError, TotStore

G, G2, ENTRY, ANN, ADMIN = 1, 2, 10, 20, 900
A, B, C, D = 101, 102, 103, 104


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.svc = TotService(load_config(), TotStore(self.dir))
        self.iid = 1000

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def open(self, guild=G, start=True):
        await self.svc.setup(guild, ENTRY, ANN, ADMIN)
        ev = await self.svc.open_event(guild, ADMIN)
        if start:
            await self.svc.start(guild, ADMIN)
        return ev["id"]

    async def search(self, who, target, part, guild=G):
        self.iid += 1
        return await self.svc.search(guild, who, target, part, self.iid)

    def player(self, uid, guild=G):
        return self.svc.current_event(guild)["players"][str(uid)]

    def other_part(self, uid, n=0):
        """不是该玩家藏糖位置的第 n 个部位。"""
        return [p for p in PART_IDS if p != self.player(uid)["hiding_part"]][n]


class RulesTests(Base):
    def test_parts_fixed(self):
        self.assertEqual([t for _, t in BODY_PARTS], ["嘴巴里", "胸口", "腋下", "耳后", "内裤里", "屁股缝", "袜子里"])
        self.assertEqual(len(set(PART_IDS)), 7)
        self.assertNotIn("navel", PART_IDS)

    async def test_join_once_and_fixed_part(self):
        eid = await self.open(start=False)
        p = await self.svc.join(G, eid, A, "behind_ear")
        self.assertEqual((p["hiding_part"], p["attempts_remaining"], p["state"]), ("behind_ear", 3, JOINED))
        with self.assertRaises(TotError):
            await self.svc.join(G, eid, A, "mouth")                       # 不能重新藏糖
        self.assertEqual(self.player(A)["hiding_part"], "behind_ear")
        with self.assertRaises(TotError):
            await self.svc.join(G, eid, B, "navel")                       # 非法部位
        self.assertNotIn(str(B), self.svc.current_event(G)["players"])

    async def test_concurrent_join_only_once(self):
        eid = await self.open(start=False)
        res = await asyncio.gather(*(self.svc.join(G, eid, A, p) for p in ("mouth", "chest", "sock")),
                                   return_exceptions=True)
        self.assertEqual(sum(not isinstance(r, Exception) for r in res), 1)

    async def test_late_join(self):
        eid = await self.open()
        await self.svc.join(G, eid, A, "mouth")                           # 默认允许补报
        await self.svc.set_late_join(G, False, ADMIN)
        with self.assertRaises(TotError):
            await self.svc.join(G, eid, B, "mouth")

    async def test_global_three_attempts_and_same_target_different_parts(self):
        eid = await self.open()
        for u, part in ((A, "mouth"), (B, "sock"), (C, "chest")):
            await self.svc.join(G, eid, u, part)
        await self.search(A, B, "mouth")
        await self.search(A, B, "chest")                                  # 同一目标换部位
        rec, me, _ = await self.search(A, C, "mouth")                     # 3 次是全局的
        self.assertEqual(me["attempts_remaining"], 0)
        with self.assertRaisesRegex(TotError, "用完"):
            await self.search(A, C, "armpit")
        self.assertEqual(len(self.svc.current_event(G)["searches"]), 3)

    async def test_rejections_do_not_cost(self):
        eid = await self.open()
        for u, part in ((A, "mouth"), (B, "sock"), (C, "chest")):
            await self.svc.join(G, eid, u, part)
        await self.search(A, B, "mouth")
        cases = [(A, A, "chest", "自己"), (C, B, "mouth", "搜过"), (A, D, "mouth", "没有报名"),
                 (D, A, "chest", "还没有报名")]
        for who, target, part, msg in cases:
            with self.subTest(msg), self.assertRaisesRegex(TotError, msg):
                await self.search(who, target, part)
        self.assertEqual((self.player(A)["attempts_remaining"], self.player(C)["attempts_remaining"]), (2, 3))
        await self.search(C, B, "sock")                                   # B 被 C 找到
        with self.assertRaisesRegex(TotError, "已经被别人找到"):
            await self.search(A, B, "chest")
        self.assertEqual(self.player(A)["attempts_remaining"], 2)

    async def test_hit_costs_one_and_both_matched(self):
        eid = await self.open()
        for u, part in ((A, "mouth"), (B, "butt_crack"), (C, "chest"), (D, "sock")):
            await self.svc.join(G, eid, u, part)
        rec, me, _ = await self.search(A, B, "butt_crack")
        self.assertTrue(rec["hit"])
        self.assertEqual(me["attempts_remaining"], 2)
        self.assertEqual((self.player(B)["state"], self.player(B)["found_by"]), (FOUND, str(A)))
        await self.search(B, C, "chest")                                  # 被找到后仍能搜
        await self.search(A, D, "sock")                                   # 一人找到多人
        ev = self.svc.current_event(G)
        self.assertEqual(matched_ids(ev), {str(A), str(B), str(C), str(D)})
        st = stats(ev)
        self.assertEqual((st["found"], st["matched"], st["hunters"], st["hunter_count"]), (3, 4, [str(A)], 2))

    async def test_exhausted_player_can_still_be_found(self):
        eid = await self.open()
        for u, part in ((A, "mouth"), (B, "sock"), (C, "chest")):
            await self.svc.join(G, eid, u, part)
        for part in ("chest", "armpit", "sock"):
            await self.search(A, B, part) if part != "sock" else await self.search(A, C, "sock")
        self.assertEqual(self.player(A)["attempts_remaining"], 0)
        rec, _, _ = await self.search(B, A, "mouth")
        self.assertTrue(rec["hit"])

    async def test_concurrent_same_part_single_winner(self):
        eid = await self.open()
        for u, part in ((A, "mouth"), (B, "sock"), (C, "chest")):
            await self.svc.join(G, eid, u, part)
        res = await asyncio.gather(self.search(A, C, "chest"), self.search(B, C, "chest"), return_exceptions=True)
        ok = [r for r in res if not isinstance(r, Exception)]
        self.assertEqual(len(ok), 1)
        self.assertIsInstance([r for r in res if isinstance(r, Exception)][0], TotError)
        winner = ok[0][0]["searcher"]
        loser = str(B) if winner == str(A) else str(A)
        self.assertEqual(self.player(C)["found_by"], winner)
        self.assertEqual(self.player(int(loser))["attempts_remaining"], 3)   # 失败方不扣次数
        # 同一目标不同部位并发：各自生效
        await self.svc.join(G, eid, D, "armpit")
        a, b = self.other_part(D, 0), self.other_part(D, 1)
        res = await asyncio.gather(self.search(A, D, a), self.search(B, D, b))
        self.assertEqual(len(res), 2)

    async def test_interaction_replay_not_charged(self):
        eid = await self.open()
        await self.svc.join(G, eid, A, "mouth")
        await self.svc.join(G, eid, B, "sock")
        r1 = await self.svc.search(G, A, B, "chest", 555)
        r2 = await self.svc.search(G, A, B, "chest", 555)
        self.assertTrue(r2[2])
        self.assertEqual(r1[0]["id"], r2[0]["id"])
        self.assertEqual(self.player(A)["attempts_remaining"], 2)
        self.assertEqual(len(self.svc.pending(G)), 1)

    async def test_withdraw_and_restore(self):
        eid = await self.open()
        await self.svc.join(G, eid, A, "mouth")
        await self.svc.join(G, eid, B, "sock")
        await self.search(A, B, "chest")
        await self.svc.withdraw(G, B)
        with self.assertRaisesRegex(TotError, "退出"):
            await self.search(A, B, "mouth")
        with self.assertRaisesRegex(TotError, "退出"):
            await self.search(B, A, "mouth")
        with self.assertRaisesRegex(TotError, "联系管理员"):
            await self.svc.join(G, eid, B, "mouth")                       # 不能借退出重置
        self.assertEqual(stats(self.svc.current_event(G))["withdrawn"], 1)
        await self.svc.restore(G, B, ADMIN)
        self.assertEqual((self.player(B)["state"], self.player(B)["hiding_part"]), (JOINED, "sock"))
        actions = [a["action"] for a in self.svc.store.load(G)["audit"]]
        self.assertIn("RESTORE", actions)

    async def test_states(self):
        eid = await self.open(start=False)
        await self.svc.join(G, eid, A, "mouth")
        await self.svc.join(G, eid, B, "sock")
        with self.assertRaisesRegex(TotError, "还没开始"):
            await self.search(A, B, "sock")
        await self.svc.start(G, ADMIN)
        with self.assertRaises(TotError):
            await self.svc.open_event(G, ADMIN)                           # 同时只能有一轮
        ev = await self.svc.close(G, ADMIN)
        self.assertEqual(ev["state"], CLOSED)
        with self.assertRaises(TotError):
            await self.search(A, B, "sock")
        with self.assertRaises(TotError):
            await self.svc.join(G, eid, C, "sock")
        eid2 = (await self.svc.open_event(G, ADMIN))["id"]                # 新一轮不带旧数据
        self.assertNotEqual(eid, eid2)
        self.assertEqual(self.svc.current_event(G)["players"], {})
        self.assertEqual(len(self.svc.event(G, eid)["players"]), 2)

    async def test_open_requires_setup(self):
        with self.assertRaisesRegex(TotError, "setup"):
            await self.svc.open_event(G, ADMIN)


class PersistenceTests(Base):
    async def test_restart_keeps_everything(self):
        eid = await self.open()
        await self.svc.join(G, eid, A, "mouth")
        await self.svc.join(G, eid, B, "sock")
        await self.search(A, B, "chest")
        await self.search(A, B, "sock")
        fresh = TotService(load_config(), TotStore(self.dir))
        ev = fresh.current_event(G)
        self.assertEqual(ev["state"], ACTIVE)
        self.assertEqual(ev["players"][str(A)]["attempts_remaining"], 1)
        self.assertEqual(ev["players"][str(B)]["found_by"], str(A))
        self.assertEqual(matched_ids(ev), {str(A), str(B)})
        self.assertEqual(len(fresh.pending(G)), 2)

    async def test_guild_isolation(self):
        e1 = await self.open(G)
        e2 = await self.open(G2)
        await self.svc.join(G, e1, A, "mouth")
        await self.svc.join(G, e1, B, "sock")
        await self.svc.join(G2, e2, A, "chest")
        await self.svc.join(G2, e2, B, "mouth")
        await self.search(A, B, "sock", guild=G)
        self.assertEqual(self.player(B, G2)["state"], JOINED)
        self.assertEqual(self.player(A, G2)["attempts_remaining"], 3)
        with self.assertRaises(TotError):
            await self.search(A, C, "sock", guild=G2)

    async def test_corrupt_file_not_overwritten(self):
        path = self.dir / f"{G}.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(StorageError):
            await self.svc.setup(G, ENTRY, ANN, ADMIN)
        self.assertEqual(path.read_text(encoding="utf-8"), "{oops")


class SecrecyTests(Base):
    async def test_hidden_part_never_public(self):
        eid = await self.open()
        await self.svc.join(G, eid, A, "underwear")
        await self.svc.join(G, eid, B, "behind_ear")
        await self.search(A, B, "mouth")
        ev = self.svc.current_event(G)
        public = [ui.board(ev).description, ui.prizes(ev, str(C))]
        public += [ui.announcement(o).description for _, o in self.svc.pending(G)]
        await self.svc.close(G, ADMIN)
        public += [ui.announcement(o).description for _, o in self.svc.pending(G)]
        for text in public:
            self.assertNotIn("内裤里", text)
            self.assertNotIn("耳后", text)
        audit = str(self.svc.store.load(G)["audit"])
        self.assertNotIn("underwear", audit)
        self.assertIn("内裤里", ui.status(self.svc.event(G, eid), str(A)))   # 本人可见

    async def test_announcement_texts(self):
        eid = await self.open()
        await self.svc.join(G, eid, A, "mouth")
        await self.svc.join(G, eid, B, "butt_crack")
        await self.search(A, B, "armpit")
        await self.search(A, B, "butt_crack")
        miss, hit = [ui.announcement(o) for _, o in self.svc.pending(G)]
        self.assertIn(f"<@{B}> 已排除：腋下（1 / 7）", miss.description)
        self.assertIn("剩余搜查：2 / 3", miss.description)
        self.assertIn(f"<@{B}> 成为 <@{A}> 的万圣节奖品", hit.description)
        self.assertIn("「屁股缝」", hit.description)
        self.assertTrue(miss.footer.text.startswith("公告 #o"))


# ---------------------------------------------------------------- outbox 发送

class FakeChannel:
    def __init__(self, cid=ANN):
        self.id, self.sent, self.fail, self.old = cid, [], 0, []

    async def send(self, content=None, embed=None, allowed_mentions=None, view=None):
        if self.fail:
            self.fail -= 1
            raise discord.HTTPException(NS(status=500, reason="boom"), "boom")
        self.sent.append(embed)
        return NS(id=5000 + len(self.sent))

    def history(self, limit=50):
        async def gen():
            for m in self.old:
                yield m
        return gen()


class OutboxTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ch = FakeChannel()
        bot = NS(user=NS(id=999), get_channel=lambda cid: self.ch if cid == ANN else None)
        self.cog = TrickOrTreat(bot, self.svc)
        eid = await self.open()
        await self.svc.join(G, eid, A, "mouth")
        await self.svc.join(G, eid, B, "sock")

    async def test_failure_keeps_result_and_retries_in_order(self):
        self.ch.fail = 1
        await self.search(A, B, "chest")
        await self.search(A, B, "armpit")
        note = await self.cog.flush(G)
        self.assertIn("补发", note)
        self.assertEqual(self.ch.sent, [])
        self.assertEqual(self.player(A)["attempts_remaining"], 1)       # 搜查不回滚
        self.assertIsNone(await self.cog.flush(G))
        self.assertIn("已排除：胸口（1 / 7）", self.ch.sent[0].description)   # 顺序没乱
        self.assertIn("已排除：胸口、腋下（2 / 7）", self.ch.sent[1].description)
        self.assertEqual(self.svc.pending(G), [])

    async def test_crash_after_send_is_not_duplicated(self):
        await self.search(A, B, "chest")
        (ev, item), = self.svc.pending(G)
        await self.svc.mark_attempt(G, ev["id"], item["id"])             # 模拟：已尝试、已发出但没记录
        embed = ui.announcement(item)
        self.ch.old = [NS(id=7777, author=NS(id=999), embeds=[embed])]
        await self.cog.flush(G)
        self.assertEqual(self.ch.sent, [])
        sent = self.svc.current_event(G)["outbox"][0]
        self.assertEqual((sent["status"], sent["message_id"]), ("sent", "7777"))

    async def test_gives_up_after_max_retries_then_admin_requeue(self):
        self.svc.cfg = dict(self.svc.cfg, outbox_max_retries=2)
        await self.search(A, B, "chest")
        self.ch.fail = 5
        await self.cog.flush(G)
        await self.cog.flush(G)
        self.assertEqual((len(self.svc.pending(G)), len(self.svc.failed(G))), (0, 1))
        self.ch.fail = 0
        self.assertEqual(await self.svc.requeue_failed(G, ADMIN), 1)
        await self.cog.flush(G)
        self.assertEqual(len(self.ch.sent), 1)

    async def test_close_posts_result(self):
        await self.search(A, B, "sock")
        await self.svc.close(G, ADMIN)
        await self.cog.flush(G)
        self.assertEqual(self.ch.sent[-1].title, "🎃 万圣夜寻糖 · 最终结果")
        self.assertIn(f"糖果猎人：<@{A}>（1 颗）", self.ch.sent[-1].description)


if __name__ == "__main__":
    unittest.main()

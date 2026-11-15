"""万象娃娃机测试：python -m unittest discover tests"""
import asyncio
import copy
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import discord

from minigames.claw_machine import engine, protocol
from minigames.claw_machine import renderer as ui
from minigames.claw_machine.cog import ClawMachine
from minigames.claw_machine.service import ClawError, ClawService
from minigames.claw_machine.store import ClawStore, StorageError

G, CH, TICKET_BOT, OTHER_BOT = 1, 500, 9000, 9001
A, B, C = 111111111111111111, 222222222222222222, 333333333333333333
CFG = engine.load_config()


def ticket_text(user=A, event="EV1", version="1"):
    return (f"🎟️ 南域游乐场 · 万象娃娃机\n\n<@{user}> 使用了一张游乐园门票！\n\n"
            f"[YUNZHOU_CLAW_START]\nVERSION={version}\nUSER_ID={user}\nEVENT_ID={event}")


def by_name(field, name):
    return next(k for k, v in CFG[engine.POOLS[field]].items() if v == name)


def cand(subject="凡人", mood="正在摆烂的", toy="毛绒团子", quirk="脑门上贴着一张镇妖符",
         grip="flat", position="center"):
    c = {"subject": by_name("subject", subject), "mood": by_name("mood", mood), "toy": by_name("toy", toy),
         "quirk": by_name("quirk", quirk), "grip": grip, "position": position}
    c["names"] = {f: CFG[engine.POOLS[f]][c[f]] for f in engine.FIELDS}
    return c


class FixedRng:
    """choice 走种子随机，randrange 返回固定值，用来精确控制成败。"""

    def __init__(self, roll=0, seed=1):
        self.roll, self.r = roll, random.Random(seed)

    def choice(self, seq):
        return self.r.choice(seq)

    def randrange(self, n):
        return self.roll


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


# ---------------------------------------------------------------------- 协议

class ProtocolTests(unittest.TestCase):
    def test_valid(self):
        t = protocol.parse_text(ticket_text())
        self.assertEqual((t.user_id, t.event_id), (A, "EV1"))

    def test_wrong_version_or_missing(self):
        self.assertIsNone(protocol.parse_text(ticket_text(version="2")))
        self.assertIsNone(protocol.parse_text("[YUNZHOU_CLAW_START]\nVERSION=1\nUSER_ID=123\n"))
        self.assertIsNone(protocol.parse_text("没有标记\nVERSION=1\nUSER_ID=111111111111111111\nEVENT_ID=x"))
        self.assertIsNone(protocol.parse_text(ticket_text(user="abc")))

    def test_embed_description(self):
        msg = NS(content="", embeds=[NS(description=ticket_text(event="E-9"))])
        self.assertEqual(protocol.parse_message(msg).event_id, "E-9")


# ---------------------------------------------------------------------- 规则

class EngineTests(unittest.TestCase):
    def test_roll_candidate_has_all_fields(self):
        rng = random.Random(3)
        for _ in range(200):
            c = engine.roll_candidate(CFG, rng)
            for f in engine.FIELDS:
                self.assertIn(c[f], CFG[engine.POOLS[f]])
                self.assertEqual(c["names"][f], CFG[engine.POOLS[f]][c[f]])
            self.assertIn(c["grip"], CFG["grips"])
            self.assertIn(c["position"], CFG["positions"])

    def test_pool_sizes(self):
        self.assertEqual([len(CFG[k]) for k in ("subjects", "moods", "toy_types", "quirks")], [20, 30, 15, 31])

    def test_name_format(self):
        c = cand("青楼老板娘", "偷薯饼的", "毛绒团子", "一捏就大喊「薯饼是我的！」")
        self.assertEqual(engine.doll_name(c["names"]), "偷薯饼的青楼老板娘毛绒团子")

    def test_chance_table(self):
        expect = {("flat", "center", "front"): 86, ("flat", "center", "side"): 50, ("flat", "center", "bottom"): 65,
                  ("protrusion", "edge", "side"): 90, ("protrusion", "pile", "side"): 57,
                  ("smooth", "edge", "bottom"): 84, ("smooth", "pile", "front"): 56,
                  ("smooth", "pile", "side"): 40, ("flat", "pile", "side"): 40}
        for (g, p, t), v in expect.items():
            self.assertEqual(engine.chance(CFG, g, p, t), v, (g, p, t))

    def test_chance_bounds(self):
        for g in CFG["grips"]:
            for p in CFG["positions"]:
                for t in CFG["techniques"]:
                    self.assertTrue(40 <= engine.chance(CFG, g, p, t) <= 90)
        self.assertEqual(engine.chance(CFG, "protrusion", "edge", "side"), 90)   # 82+8=90 上限
        self.assertEqual(engine.chance(CFG, "smooth", "pile", "side"), 40)       # 60-25=35 → 40

    def test_best_technique_varies(self):
        best = {(g, p): max(CFG["techniques"], key=lambda t: engine.chance(CFG, g, p, t))
                for g in CFG["grips"] for p in CFG["positions"]}
        self.assertEqual(set(best.values()), {"front", "side", "bottom"})

    def test_all_six_eggs(self):
        cases = {
            "eternal_resident": cand("五舅", "秘境外打地铺的", "毛绒睡袋", "口袋里藏着一张假藏宝图"),
            "potato_thief": cand("青楼老板娘", "偷薯饼的", "八音盒", "突然长出了九条尾巴"),
            "debt_no_worry": cand("小小酥", "沉迷惊鸿鉴的", "不倒翁", "半夜偷偷登录惊鸿鉴"),
            "long_rang_jun": cand("女帝", "一脸智慧的", "钥匙挂件", "总把龙骧军念成龙嚷军"),
            "snowball_grows": cand("雪球", "滚雪球的"),
            "hiss_warning": cand("玉猫", "正在哈气的"),
        }
        for egg_id, c in cases.items():
            self.assertEqual(engine.match_egg(CFG, c)["id"], egg_id)

    def test_near_misses_do_not_trigger(self):
        misses = [
            cand("五舅", "秘境外打地铺的", "毛绒团子"),            # 少了睡袋
            cand("五舅", "正在摆烂的", "毛绒睡袋"),
            cand("女帝", "一脸智慧的", "钥匙挂件", "突然长出了九条尾巴"),   # 少了龙嚷军
            cand("小小酥", "偷薯饼的"),
            cand("大大苏", "沉迷惊鸿鉴的"),
            cand("玉猫", "正在踩奶的"),
            cand("雪球", "正在哈气的"),
        ]
        for c in misses:
            self.assertIsNone(engine.match_egg(CFG, c), c["names"])

    def test_most_specific_then_priority(self):
        cfg = copy.deepcopy(CFG)
        cfg["easter_eggs"] = [
            {"id": "two", "title": "2", "priority": 9, "match": {"subject": "玉猫", "mood": "正在哈气的"}},
            {"id": "three", "title": "3", "priority": 0,
             "match": {"subject": "玉猫", "mood": "正在哈气的", "toy": "毛绒团子"}},
            {"id": "three_hi", "title": "3h", "priority": 5,
             "match": {"subject": "玉猫", "mood": "正在哈气的", "quirk": "脑门上贴着一张镇妖符"}},
        ]
        engine.prepare(cfg)
        self.assertEqual(engine.match_egg(cfg, cand("玉猫", "正在哈气的", "毛绒团子", "脑门上贴着一张镇妖符"))["id"], "three_hi")
        self.assertEqual(engine.match_egg(cfg, cand("玉猫", "正在哈气的", "毛绒团子", "半夜偷偷登录惊鸿鉴"))["id"], "three")
        self.assertEqual(engine.match_egg(cfg, cand("玉猫", "正在哈气的", "八音盒", "半夜偷偷登录惊鸿鉴"))["id"], "two")

    def test_bad_egg_config(self):
        cfg = copy.deepcopy(CFG)
        cfg["easter_eggs"] = [{"id": "x", "title": "x", "match": {"subject": "不存在", "mood": "偷薯饼的"}}]
        with self.assertRaises(engine.ConfigError):
            engine.prepare(cfg)
        cfg["easter_eggs"] = [{"id": "x", "title": "x", "match": {"subject": "玉猫"}}]
        with self.assertRaises(engine.ConfigError):
            engine.prepare(cfg)


# ---------------------------------------------------------------------- 服务

class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.rng = FixedRng(roll=0)
        self.svc = self.new_service()

    def new_service(self):
        return ClawService(CFG, ClawStore(Path(self.tmp.name)), rng=self.rng, clock=self.clock)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def game_with(self, c, user=A, event=None):
        event = event or f"E{random.random()}"
        game, _ = await self.svc.open_game(G, CH, user, event, 1)
        async with self.svc.store.transaction(G) as doc:
            doc["games"][game["id"]]["candidates"][0] = c
        return game["id"]

    async def win(self, c=None, user=A, tech="front"):
        gid = await self.game_with(c or cand(), user)
        self.rng.roll = 0
        _, doll, _ = await self.svc.grab(G, gid, user, 0, tech)
        return doll


class ServiceTests(Base):
    async def test_open_and_dedupe(self):
        g1, new1 = await self.svc.open_game(G, CH, A, "EV1", 10)
        g2, new2 = await self.svc.open_game(G, CH, A, "EV1", 11)
        self.assertTrue(new1)
        self.assertFalse(new2)
        self.assertEqual(g1["id"], g2["id"])
        self.assertEqual(len(self.svc.store.load(G)["games"]), 1)
        self.assertEqual(len(g1["candidates"]), 3)

    async def test_concurrent_same_event(self):
        res = await asyncio.gather(*[self.svc.open_game(G, CH, A, "EVX", 1) for _ in range(5)])
        self.assertEqual(sum(1 for _, new in res if new), 1)
        self.assertEqual(len(self.svc.store.load(G)["games"]), 1)

    async def test_multiple_tickets_queue(self):
        for i in range(3):
            await self.svc.open_game(G, CH, A, f"T{i}", i)
        pending = self.svc.pending_games(G, A)
        self.assertEqual([g["id"] for g in pending], ["1", "2", "3"])
        await self.svc.grab(G, "1", A, 0, "front")
        self.assertEqual([g["id"] for g in self.svc.pending_games(G, A)], ["2", "3"])

    async def test_quirk_hidden_before_grab(self):
        gid = await self.game_with(cand(quirk="总把龙骧军念成龙嚷军"))
        game = self.svc.game(G, gid)
        text = ui.pick_embed(CFG, game).description + ui.aim_embed(CFG, game, 0, "front").description
        for c in game["candidates"]:
            self.assertNotIn(c["names"]["quirk"], text)

    async def test_success_creates_doll(self):
        c = cand("玉猫", "正在哈气的", "毛绒团子", "突然长出了九条尾巴")
        doll = await self.win(c)
        self.assertEqual(doll["id"], "YZ-000001")
        self.assertEqual(doll["name"], "正在哈气的玉猫毛绒团子")
        self.assertEqual(doll["egg"]["title"], "哈气警告")
        self.assertEqual((doll["original_owner_id"], doll["current_owner_id"]), (str(A), str(A)))
        self.assertEqual(self.svc.collection(G, A)[0]["id"], doll["id"])
        embed = ui.result_embed(CFG, self.svc.game(G, doll["source_game_id"]), doll)
        self.assertIn("突然长出了九条尾巴", embed.description)
        self.assertIn("哈气警告", embed.description)
        st = self.svc.stats(G, A)
        self.assertEqual((st["plays"], st["wins"], st["holding"], st["eggs"], st["unique_combos"]), (1, 1, 1, 1, 1))

    async def test_failure(self):
        gid = await self.game_with(cand(grip="smooth", position="pile"))
        self.rng.roll = 40                                    # 成功率 40%：roll 40 不成功
        game, doll, replay = await self.svc.grab(G, gid, A, 0, "side")
        self.assertIsNone(doll)
        self.assertFalse(game["result"]["success"])
        self.assertIn(game["result"]["fail_text"], CFG["fail_texts"])
        self.assertEqual(self.svc.collection(G, A), [])
        self.assertNotIn(game["candidates"][0]["names"]["quirk"], ui.result_embed(CFG, game, None).description)
        self.assertEqual(self.svc.stats(G, A)["plays"], 1)

    async def test_boundary_roll(self):
        gid = await self.game_with(cand(grip="smooth", position="pile"))
        self.rng.roll = 39
        _, doll, _ = await self.svc.grab(G, gid, A, 0, "side")
        self.assertIsNotNone(doll)

    async def test_grab_only_once(self):
        gid = await self.game_with(cand())
        _, d1, r1 = await self.svc.grab(G, gid, A, 0, "front")
        self.rng.roll = 99
        _, d2, r2 = await self.svc.grab(G, gid, A, 1, "side")
        self.assertFalse(r1)
        self.assertTrue(r2)
        self.assertEqual(d1["id"], d2["id"])
        self.assertEqual(len(self.svc.store.load(G)["dolls"]), 1)
        self.assertEqual(self.svc.stats(G, A)["plays"], 1)

    async def test_concurrent_grab(self):
        gid = await self.game_with(cand())
        res = await asyncio.gather(*[self.svc.grab(G, gid, A, 0, "front") for _ in range(5)])
        self.assertEqual(sum(1 for _, _, replay in res if not replay), 1)
        self.assertEqual(len(self.svc.store.load(G)["dolls"]), 1)

    async def test_other_player_blocked(self):
        gid = await self.game_with(cand())
        with self.assertRaises(ClawError):
            await self.svc.grab(G, gid, B, 0, "front")
        with self.assertRaises(ClawError):
            self.svc.own_game(G, gid, B)
        self.assertIsNone(self.svc.game(G, gid)["result"])

    async def test_bad_client_input(self):
        gid = await self.game_with(cand())
        with self.assertRaises(ClawError):
            await self.svc.grab(G, gid, A, 5, "front")
        with self.assertRaises(ClawError):
            await self.svc.grab(G, gid, A, 0, "kick")

    async def test_restart_restores(self):
        g, _ = await self.svc.open_game(G, CH, A, "EVR", 1)
        svc2 = self.new_service()
        g2 = svc2.game(G, g["id"])
        self.assertEqual(g2["candidates"], g["candidates"])
        _, again = await svc2.open_game(G, CH, A, "EVR", 1)
        self.assertFalse(again)

    async def test_duplicate_combo_distinct_ids(self):
        d1 = await self.win(cand())
        d2 = await self.win(cand())
        self.assertNotEqual(d1["id"], d2["id"])
        self.assertEqual(d1["name"], d2["name"])
        self.assertEqual(self.svc.stats(G, A)["unique_combos"], 1)

    async def test_snapshot_survives_rename(self):
        doll = await self.win(cand("凡人"))
        cfg = copy.deepcopy(CFG)
        del cfg["subjects"][doll["subject"]]                       # 词条被删掉
        svc2 = ClawService(engine.prepare(cfg), ClawStore(Path(self.tmp.name)), rng=self.rng, clock=self.clock)
        self.assertEqual(svc2.doll(G, doll["id"])["name"], "正在摆烂的凡人毛绒团子")

    async def test_showcase_limit(self):
        dolls = [await self.win() for _ in range(6)]
        for d in dolls[:5]:
            await self.svc.set_showcase(G, A, d["id"], True)
        with self.assertRaises(ClawError):
            await self.svc.set_showcase(G, A, dolls[5]["id"], True)
        await self.svc.set_showcase(G, A, dolls[0]["id"], False)
        await self.svc.set_showcase(G, A, dolls[5]["id"], True)
        self.assertEqual(len(self.svc.showcase(G, A)), 5)
        with self.assertRaises(ClawError):
            await self.svc.set_showcase(G, B, dolls[1]["id"], True)

    async def test_pagination_and_filter(self):
        for i in range(10):
            self.clock.t += timedelta(minutes=1)
            await self.win(cand("玉猫", "正在哈气的") if i % 3 == 0 else cand())
        page0, p, pages = self.svc.page(G, A, "all", 0)
        self.assertEqual((len(page0), p, pages), (8, 0, 2))
        self.assertEqual(page0[0]["id"], "YZ-000010")                # 最新在前
        page1, p, _ = self.svc.page(G, A, "all", 5)
        self.assertEqual((len(page1), p), (2, 1))
        eggs = self.svc.collection(G, A, "eggs")
        self.assertEqual(len(eggs), 4)
        self.assertTrue(all(d["egg"] for d in eggs))
        await self.svc.set_showcase(G, A, "YZ-000002", True)
        self.assertEqual([d["id"] for d in self.svc.collection(G, A, "showcase")], ["YZ-000002"])


class GiftTests(Base):
    async def test_accept_transfers(self):
        doll = await self.win(cand("玉猫", "正在哈气的"))
        await self.svc.set_showcase(G, A, doll["id"], True)
        gift = await self.svc.create_gift(G, A, doll["id"], B)
        with self.assertRaises(ClawError):
            await self.svc.respond_gift(G, gift["id"], C, "accept")
        gift, d, changed = await self.svc.respond_gift(G, gift["id"], B, "accept")
        self.assertTrue(changed)
        self.assertEqual(gift["status"], "accepted")
        self.assertEqual((d["original_owner_id"], d["current_owner_id"]), (str(A), str(B)))
        self.assertFalse(d["is_showcased"])
        self.assertEqual(self.svc.collection(G, A), [])
        self.assertEqual(self.svc.collection(G, B)[0]["id"], doll["id"])
        a, b = self.svc.stats(G, A), self.svc.stats(G, B)
        self.assertEqual((a["wins"], a["eggs"], a["unique_combos"], a["holding"], a["gifts_sent"]), (1, 1, 1, 0, 1))
        self.assertEqual((b["wins"], b["eggs"], b["holding"], b["holding_gifted"], b["gifts_received"]), (0, 0, 1, 1, 1))
        self.assertIn("哈气警告", ui.gift_done_embed(gift, d).description)
        _, _, again = await self.svc.respond_gift(G, gift["id"], B, "accept")
        self.assertFalse(again)

    async def test_decline_and_cancel_unlock(self):
        doll = await self.win()
        g1 = await self.svc.create_gift(G, A, doll["id"], B)
        g1, d, _ = await self.svc.respond_gift(G, g1["id"], B, "decline")
        self.assertEqual((g1["status"], d["current_owner_id"], d["pending_gift"]), ("declined", str(A), None))
        g2 = await self.svc.create_gift(G, A, doll["id"], B)
        with self.assertRaises(ClawError):
            await self.svc.respond_gift(G, g2["id"], B, "cancel")
        g2, d, _ = await self.svc.respond_gift(G, g2["id"], A, "cancel")
        self.assertEqual((g2["status"], d["pending_gift"]), ("cancelled", None))

    async def test_rules(self):
        doll = await self.win()
        with self.assertRaises(ClawError):
            await self.svc.create_gift(G, A, doll["id"], A)
        with self.assertRaises(ClawError):
            await self.svc.create_gift(G, B, doll["id"], C)
        with self.assertRaises(ClawError):
            await self.svc.create_gift(G, A, doll["id"], C, to_is_bot=True)
        await self.svc.create_gift(G, A, doll["id"], B)
        with self.assertRaises(ClawError):
            await self.svc.create_gift(G, A, doll["id"], C)            # 已锁定

    async def test_concurrent_gifts_only_one(self):
        doll = await self.win()
        res = await asyncio.gather(self.svc.create_gift(G, A, doll["id"], B),
                                   self.svc.create_gift(G, A, doll["id"], C), return_exceptions=True)
        self.assertEqual(sum(1 for r in res if isinstance(r, dict)), 1)
        self.assertEqual(sum(1 for r in res if isinstance(r, ClawError)), 1)

    async def test_concurrent_accept_and_cancel(self):
        doll = await self.win()
        gift = await self.svc.create_gift(G, A, doll["id"], B)
        res = await asyncio.gather(self.svc.respond_gift(G, gift["id"], B, "accept"),
                                   self.svc.respond_gift(G, gift["id"], A, "cancel"))
        self.assertEqual(sum(1 for _, _, changed in res if changed), 1)
        final = self.svc.gift(G, gift["id"])
        owner = self.svc.doll(G, doll["id"])["current_owner_id"]
        self.assertEqual(owner, str(B) if final["status"] == "accepted" else str(A))

    async def test_expiry(self):
        doll = await self.win()
        gift = await self.svc.create_gift(G, A, doll["id"], B)
        self.clock.t += timedelta(hours=25)
        expired = await self.svc.expire_gifts(G)
        self.assertEqual([g["id"] for g in expired], [gift["id"]])
        self.assertIsNone(self.svc.doll(G, doll["id"])["pending_gift"])
        g, d, _ = await self.svc.respond_gift(G, gift["id"], B, "accept")
        self.assertEqual((g["status"], d["current_owner_id"]), ("expired", str(A)))

    async def test_lazy_expiry_on_accept_and_regift(self):
        doll = await self.win()
        gift = await self.svc.create_gift(G, A, doll["id"], B)
        self.clock.t += timedelta(hours=25)
        g, d, changed = await self.svc.respond_gift(G, gift["id"], B, "accept")
        self.assertEqual((g["status"], d["current_owner_id"], changed), ("expired", str(A), True))
        await self.svc.create_gift(G, A, doll["id"], C)

    async def test_history_kept_after_gift_chain(self):
        doll = await self.win()
        g = await self.svc.create_gift(G, A, doll["id"], B)
        await self.svc.respond_gift(G, g["id"], B, "accept")
        g = await self.svc.create_gift(G, B, doll["id"], C)
        _, d, _ = await self.svc.respond_gift(G, g["id"], C, "accept")
        self.assertEqual(d["original_owner_id"], str(A))
        self.assertEqual([(h["from"], h["to"]) for h in d["history"]], [(str(A), str(B)), (str(B), str(C))])
        self.assertEqual(self.svc.stats(G, A)["wins"], 1)


# ---------------------------------------------------------------------- Cog（Discord 交互）

class FakeChannel:
    def __init__(self, cid=CH, parent_id=None):
        self.id, self.parent_id, self.sent, self.n = cid, parent_id, [], 7000

    async def send(self, content=None, **kw):
        self.n += 1
        msg = NS(id=self.n, channel=self, content=content, **kw)
        self.sent.append(msg)
        return msg


def message(author, content, channel=None, guild=G, mid=1):
    ch = channel or FakeChannel()
    return NS(id=mid, author=NS(id=author, bot=True), guild=NS(id=guild) if guild else None, channel=ch,
              content=content, embeds=[], to_reference=lambda **kw: None)


class FakeResponse:
    def __init__(self):
        self.calls, self._done = [], False

    def is_done(self):
        return self._done

    async def send_message(self, *a, **kw):
        self._done = True
        self.calls.append(("send", a, kw))

    async def edit_message(self, **kw):
        self._done = True
        self.calls.append(("edit", (), kw))

    async def defer(self, **kw):
        self._done = True
        self.calls.append(("defer", (), kw))


def interaction(user, msg_id=0):
    it = NS(guild_id=G, user=NS(id=user), response=FakeResponse(), message=NS(id=msg_id), edits=[], follow=[])

    async def edit_original_response(**kw):
        it.edits.append(kw)

    async def followup_send(*a, **kw):
        it.follow.append((a, kw))
    it.edit_original_response = edit_original_response
    it.followup = NS(send=followup_send)
    return it


class CogTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        cfg = copy.deepcopy(CFG)
        cfg["trusted_bot_ids"], cfg["channel_ids"] = [TICKET_BOT], [CH]
        self.svc.cfg = engine.prepare(cfg)
        self.channel = FakeChannel()
        self.bot = NS(get_channel=lambda cid: self.channel if cid == CH else None)
        self.cog = ClawMachine(self.bot, self.svc)

    async def test_valid_ticket_opens_and_posts(self):
        await self.cog.on_message(message(TICKET_BOT, ticket_text(), self.channel))
        games = self.svc.pending_games(G, A)
        self.assertEqual(len(games), 1)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(games[0]["panel_message_id"], str(self.channel.sent[0].id))
        self.assertEqual(self.channel.sent[0].allowed_mentions.users[0].id, A)

    async def test_untrusted_wrong_channel_wrong_version(self):
        await self.cog.on_message(message(OTHER_BOT, ticket_text(), self.channel))
        await self.cog.on_message(message(A, ticket_text(), self.channel))
        await self.cog.on_message(message(TICKET_BOT, ticket_text(), FakeChannel(cid=999)))
        await self.cog.on_message(message(TICKET_BOT, ticket_text(version="2"), self.channel))
        await self.cog.on_message(message(TICKET_BOT, ticket_text(), self.channel, guild=None))
        self.assertEqual(self.svc.store.load(G)["games"], {})
        self.assertEqual(self.channel.sent, [])

    async def test_thread_under_game_channel(self):
        await self.cog.on_message(message(TICKET_BOT, ticket_text(), FakeChannel(cid=600, parent_id=CH)))
        self.assertEqual(len(self.svc.pending_games(G, A)), 1)

    async def test_duplicate_event_posts_once(self):
        msg = message(TICKET_BOT, ticket_text(event="DUP"), self.channel)
        await asyncio.gather(self.cog.on_message(msg), self.cog.on_message(msg))
        await self.cog.on_message(msg)
        self.assertEqual(len(self.svc.store.load(G)["games"]), 1)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_multiple_tickets(self):
        for i in range(3):
            await self.cog.on_message(message(TICKET_BOT, ticket_text(event=f"M{i}"), self.channel, mid=i))
        self.assertEqual(len(self.svc.pending_games(G, A)), 3)
        self.assertEqual(len(self.channel.sent), 3)

    async def test_flow_and_ownership(self):
        await self.cog.on_message(message(TICKET_BOT, ticket_text(), self.channel))
        game = self.svc.pending_games(G, A)[0]
        panel = int(game["panel_message_id"])
        it = interaction(B, panel)
        await self.cog.handle_game(it, "pick", game["id"], 0, None)
        self.assertEqual(it.response.calls[0][0], "send")              # 别人点：只给他一条私密提示
        self.assertTrue(it.response.calls[0][2]["ephemeral"])
        it = interaction(A, panel)
        await self.cog.handle_game(it, "pick", game["id"], 0, None)
        self.assertEqual(it.response.calls[0][0], "edit")
        it = interaction(A, panel)
        await self.cog.handle_game(it, "go", game["id"], 0, "front")
        self.assertEqual(len(it.edits), 1)
        self.assertIsNotNone(self.svc.game(G, game["id"])["result"])
        it2 = interaction(A, panel)                                     # 重复点确认
        await self.cog.handle_game(it2, "go", game["id"], 1, "side")
        self.assertEqual(len(self.svc.store.load(G)["dolls"]), 1)
        self.assertEqual(it2.response.calls[0][0], "edit")              # 直接显示已保存的结果

    async def test_resume_grab_updates_public_panel(self):
        await self.cog.on_message(message(TICKET_BOT, ticket_text(), self.channel))
        game = self.svc.pending_games(G, A)[0]
        edited = []

        def partial(mid):
            async def edit(**kw):
                edited.append((mid, kw))
            return NS(edit=edit)
        self.channel.get_partial_message = partial
        it = interaction(A, msg_id=424242)                              # 私密 resume 面板
        await self.cog.handle_game(it, "go", game["id"], 0, "front")
        self.assertEqual(edited[0][0], int(game["panel_message_id"]))

    async def test_restart_does_not_reroll(self):
        await self.cog.on_message(message(TICKET_BOT, ticket_text(event="R1"), self.channel))
        before = self.svc.pending_games(G, A)[0]["candidates"]
        svc2 = self.new_service()
        svc2.cfg = self.svc.cfg
        cog2 = ClawMachine(self.bot, svc2)
        await cog2.on_message(message(TICKET_BOT, ticket_text(event="R1"), self.channel))   # 回扫到同一条
        self.assertEqual(svc2.pending_games(G, A)[0]["candidates"], before)
        self.assertEqual(len(self.channel.sent), 1)


if __name__ == "__main__":
    unittest.main()

"""RP 骰点与场次助手测试：python -m unittest discover tests"""
import asyncio
import copy
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import discord

from minigames.rp_dice import dice
from minigames.rp_dice import presentation as ui
from minigames.rp_dice.cog import RpDice
from minigames.rp_dice.service import ACTIVE, ENDED, Actor, RpError, RpService, load_config, parse_stat_input
from minigames.rp_dice.store import RpStore

CFG = load_config()
LIMITS = CFG["dice_limits"]
G, CH, CH2, THREAD, OTHER_THREAD = 1, 10, 20, 11, 21
DM, P1, P2, ADMIN, RANDO = 100, 201, 202, 300, 400


def fixed(values):
    """按顺序返回 values 里的点数（rand(n) 返回 [0, n) → 点数-1）。"""
    it = iter(values)
    return lambda n: next(it) - 1


# ---------------------------------------------------------------- 骰点引擎

class DiceTests(unittest.TestCase):
    def test_parse_cases(self):
        cases = {
            "1d100>98": (1, 100, ">", 98), "1d10>5": (1, 10, ">", 5), "1D20 >= 15": (1, 20, ">=", 15),
            "1d100<30": (1, 100, "<", 30), "1d6": (1, 6, None, None), "2d6": (2, 6, None, None),
            "  1d100 > 98  ": (1, 100, ">", 98), "3d8<=12": (3, 8, "<=", 12), "1d4==2": (1, 4, "=", 2),
            "1d4=0": (1, 4, "=", 0),
        }
        for text, (c, s, op, t) in cases.items():
            with self.subTest(text):
                e = dice.parse(text, LIMITS)
                self.assertEqual((e.count, e.sides, e.op, e.threshold), (c, s, op, t))

    def test_not_expressions(self):
        for text in ("今天掷个1d100>98", "1d100>98 好耶", "`1d100>98`", "> 1d100>98", "d20", "1d", "1d100>",
                     "1d100>-5", "1d100+5", "2d6 2d6", ""):
            with self.subTest(text):
                self.assertIsNone(dice.parse(text, LIMITS))

    def test_limits(self):
        for text in ("21d6", "0d6", "1d1", "1d1001", "1d100>20001"):
            with self.subTest(text), self.assertRaises(dice.DiceError):
                dice.parse(text, LIMITS)
        self.assertIsNotNone(dice.parse("20d1000>20000", LIMITS))

    def test_boundaries(self):
        e = dice.parse("1d100>98", LIMITS)
        self.assertFalse(dice.roll(e, fixed([98])).success)                # 恰好 98：> 失败
        self.assertTrue(dice.roll(e, fixed([99])).success)
        self.assertTrue(dice.roll(dice.parse("1d100>=98", LIMITS), fixed([98])).success)
        self.assertTrue(dice.roll(dice.parse("1d100<30", LIMITS), fixed([29])).success)
        self.assertFalse(dice.roll(dice.parse("1d100<30", LIMITS), fixed([30])).success)
        self.assertTrue(dice.roll(dice.parse("2d6=7", LIMITS), fixed([2, 5])).success)

    def test_format(self):
        r = dice.roll(dice.parse("1d100>98", LIMITS), fixed([99]))
        self.assertEqual(dice.format_result(r), "🎲 1d100 → 99 | ✅ 成功（99 > 98）")
        r = dice.roll(dice.parse("2d6", LIMITS), fixed([2, 5]))
        self.assertEqual(dice.format_result(r), "🎲 2d6 → [2, 5]，合计 7")
        r = dice.roll(dice.parse("1d100>98", LIMITS), fixed([98]))
        self.assertEqual(dice.format_result(r), "🎲 1d100 → 98 | ❌ 失败（需要 > 98）")
        r = dice.roll(dice.parse("1d4==2", LIMITS), fixed([2]))
        self.assertIn("（2 = 2）", dice.format_result(r))

    def test_uniform_range(self):
        e = dice.parse("1d6", LIMITS)
        counts = Counter(dice.roll(e).total for _ in range(6000))
        self.assertEqual(set(counts), set(range(1, 7)))
        self.assertTrue(all(800 < c < 1200 for c in counts.values()))


class StatInputTests(unittest.TestCase):
    def test_parse(self):
        self.assertIsNone(parse_stat_input("  "))
        self.assertEqual(parse_stat_input("-15"), ("delta", -15))
        self.assertEqual(parse_stat_input("+ 20"), ("delta", 20))
        self.assertEqual(parse_stat_input("80"), ("set", 80))
        self.assertEqual(parse_stat_input("=0"), ("set", 0))
        for bad in ("abc", "1.5", "--3", "+"):
            with self.subTest(bad), self.assertRaises(RpError):
                parse_stat_input(bad)


# ---------------------------------------------------------------- 场次服务

class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = RpService(copy.deepcopy(CFG), RpStore(Path(self.tmp.name)))
        self.dm = Actor(DM)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def session(self, channel=CH, players=(P1, P2)):
        s = await self.svc.start(G, channel, DM, None)
        await self.svc.add_players(G, s["id"], self.dm, list(players))
        return s["id"]

    def s(self, sid):
        return self.svc.store.load(G)["sessions"][sid]

    async def test_one_active_per_channel_and_isolation(self):
        sid = await self.session()
        with self.assertRaisesRegex(RpError, "已经有"):
            await self.svc.start(G, CH, DM, None)
        await self.svc.start(G, THREAD, DM, None)                            # 子区是独立频道
        await self.svc.end(G, sid, self.dm)
        await self.svc.start(G, CH, DM, 5)                                   # 结束后可再开

    async def test_initial_values_and_independent_players(self):
        sid = await self.session()
        await self.svc.change_stats(G, sid, self.dm, P1, {"sanity": "-15"})
        s = self.s(sid)
        self.assertEqual((s["participants"][str(P1)]["sanity"], s["participants"][str(P2)]["sanity"]), (85, 100))
        self.assertEqual((s["participants"][str(P2)]["heat"], s["progress"], s["progress_max"]), (0, 0, 10))

    async def test_clamp_and_actual_delta(self):
        sid = await self.session()
        await self.svc.change_stats(G, sid, self.dm, P1, {"heat": "95"})
        res = await self.svc.change_stats(G, sid, self.dm, P1, {"heat": "+20", "sanity": "-150"})
        self.assertEqual(res["changes"], {"heat": [95, 100], "sanity": [100, 0]})
        text = ui.announcement(self.s(sid), self.svc.pending(G, sid)[-1])
        self.assertIn("发情值：95 → 100（+5）", text)                          # 显示实际变化
        self.assertIn("理智：100 → 0（-100）", text)
        with self.assertRaisesRegex(RpError, "0–100"):
            await self.svc.change_stats(G, sid, self.dm, P1, {"sanity": "150"})   # 绝对值越界拒绝

    async def test_no_change_no_announcement(self):
        sid = await self.session()
        before = len(self.svc.pending(G, sid))
        self.assertIsNone(await self.svc.change_stats(G, sid, self.dm, P1, {"sanity": "+0", "heat": "0"}))
        self.assertIsNone(await self.svc.change_stats(G, sid, self.dm, P1, {"sanity": "+5"}))   # 已在上限
        self.assertIsNone(await self.svc.change_progress(G, sid, self.dm, delta=-1))            # 已在 0
        self.assertEqual(len(self.svc.pending(G, sid)), before)

    async def test_conditions(self):
        sid = await self.session()
        await self.svc.add_condition(G, sid, self.dm, P1, "  束缚 I  ")
        with self.assertRaisesRegex(RpError, "已经有"):
            await self.svc.add_condition(G, sid, self.dm, P1, "束缚 I")
        with self.assertRaisesRegex(RpError, "50"):
            await self.svc.add_condition(G, sid, self.dm, P1, "长" * 51)
        text = ui.announcement(self.s(sid), self.svc.pending(G, sid)[-1])
        self.assertIn("异常状态：束缚 I（新增：束缚 I）", text)
        await self.svc.remove_condition(G, sid, self.dm, P1, "束缚 I")
        self.assertIn("异常状态：无（解除：束缚 I）", ui.announcement(self.s(sid), self.svc.pending(G, sid)[-1]))

    async def test_progress(self):
        sid = await self.session()
        self.assertEqual(await self.svc.change_progress(G, sid, self.dm, delta=1), [0, 1])
        self.assertEqual(await self.svc.change_progress(G, sid, self.dm, set_to=10), [1, 10])
        self.assertIsNone(await self.svc.change_progress(G, sid, self.dm, delta=1))
        with self.assertRaises(RpError):
            await self.svc.change_progress(G, sid, self.dm, set_to=11)
        ann = self.svc.pending(G, sid)[-1]
        self.assertEqual(ui.announcement(self.s(sid), ann), f"🧭 探索进度：1/10 → 10/10（DM：<@{DM}>）")
        s = self.s(sid)
        self.assertEqual((s["participants"][str(P1)]["sanity"], s["participants"][str(P1)]["heat"]), (100, 0))

    async def test_permissions(self):
        sid = await self.session()
        with self.assertRaisesRegex(RpError, "DM 或管理员"):
            await self.svc.change_stats(G, sid, Actor(P1), P1, {"sanity": "+1"})
        with self.assertRaisesRegex(RpError, "DM 或管理员"):
            await self.svc.end(G, sid, Actor(RANDO))
        await self.svc.change_stats(G, sid, Actor(ADMIN, is_admin=True), P1, {"sanity": "-1"})
        with self.assertRaisesRegex(RpError, "不在本场"):
            await self.svc.change_stats(G, sid, self.dm, RANDO, {"sanity": "-1"})

    async def test_remove_and_readd_keeps_record(self):
        sid = await self.session()
        await self.svc.change_stats(G, sid, self.dm, P1, {"sanity": "-40"})
        await self.svc.remove_player(G, sid, self.dm, P1)
        self.assertEqual([u for u, _ in RpService.active_players(self.s(sid))], [str(P2)])
        with self.assertRaises(RpError):
            await self.svc.change_stats(G, sid, self.dm, P1, {"sanity": "-1"})
        self.assertEqual(await self.svc.add_players(G, sid, self.dm, [P1, P2]), [str(P1)])
        self.assertEqual(self.s(sid)["participants"][str(P1)]["sanity"], 60)

    async def test_ended_session_read_only(self):
        sid = await self.session()
        await self.svc.end(G, sid, self.dm)
        self.assertEqual(self.s(sid)["status"], ENDED)
        with self.assertRaisesRegex(RpError, "结束"):
            await self.svc.change_progress(G, sid, self.dm, delta=1)
        self.assertIsNone(self.svc.active_session(G, CH))

    async def test_audit_trail(self):
        sid = await self.session()
        await self.svc.change_stats(G, sid, self.dm, P1, {"sanity": "-10", "heat": "40"})
        types = [e["type"] for e in self.s(sid)["events"]]
        self.assertEqual(types, ["SESSION_START", "PARTICIPANT_ADD", "PARTICIPANT_ADD", "STAT_DELTA", "STAT_SET"])
        ev = self.s(sid)["events"][-1]
        self.assertEqual((ev["actor_user_id"], ev["target_user_id"], ev["before"], ev["after"]),
                         (str(DM), str(P1), 0, 40))

    async def test_concurrent_deltas_all_applied(self):
        sid = await self.session()
        await asyncio.gather(*(self.svc.change_stats(G, sid, self.dm, P1, {"sanity": "-3"}) for _ in range(10)))
        self.assertEqual(self.s(sid)["participants"][str(P1)]["sanity"], 70)

    async def test_restart_keeps_session(self):
        sid = await self.session()
        await self.svc.change_stats(G, sid, self.dm, P2, {"heat": "+30"})
        svc2 = RpService(copy.deepcopy(CFG), RpStore(Path(self.tmp.name)))
        s = svc2.require_active(G, CH)
        self.assertEqual((s["id"], s["participants"][str(P2)]["heat"], s["status"]), (sid, 30, ACTIVE))

    async def test_status_embeds_split(self):
        s = await self.svc.start(G, CH, DM, None)
        await self.svc.add_players(G, s["id"], self.dm, list(range(1000, 1025)))
        await self.svc.add_condition(G, s["id"], self.dm, 1000, "**粗体**")
        sess = self.s(s["id"])
        embeds = ui.status_embeds(sess, RpService.active_players(sess))
        self.assertTrue(all(len(e) <= 6000 for e in embeds))
        blob = " ".join(f.value for e in embeds for f in e.fields)
        self.assertEqual(sum(f"<@{u}>" in blob for u in range(1000, 1025)), 25)
        self.assertIn(r"\*\*粗体\*\*", blob)


# ---------------------------------------------------------------- Discord 层

class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []
        self.fail = False

    async def send(self, content=None, allowed_mentions=None, **kw):
        if self.fail:
            raise discord.HTTPException(NS(status=500, reason="x"), "x")
        self.sent.append((content, allowed_mentions))
        return NS(id=900 + len(self.sent))


def message(content, channel=CH, parent=None, author=P1, bot=False, webhook=None, mid=1,
            mtype=discord.MessageType.default):
    msg = NS(content=content, guild=NS(id=G), channel=NS(id=channel, parent_id=parent), id=mid, type=mtype,
             author=NS(id=author, bot=bot), webhook_id=webhook, replies=[])

    async def reply(text, mention_author=True, allowed_mentions=None):
        msg.replies.append((text, mention_author, allowed_mentions))
    msg.reply = reply
    return msg


def perms(**kw):
    base = dict(administrator=False, manage_guild=False, manage_channels=False)
    base.update(kw)
    return NS(**base)


class FakeInteraction:
    def __init__(self, uid=DM, channel=CH, roles=(), itype=discord.InteractionType.application_command, **perm):
        self.user = NS(id=uid, roles=[NS(id=r) for r in roles], mention=f"<@{uid}>")
        self.guild_id, self.channel_id, self.guild = G, channel, NS(get_member=lambda i: None)
        self.permissions = perms(**perm)
        self.type = itype
        self.data = {}
        self.sent, self.edits, self.modal, self._done = [], [], None, False
        self.response = NS(is_done=lambda: self._done, send_message=self._send, defer=self._defer,
                           edit_message=self._edit, send_modal=self._modal)
        self.followup = NS(send=self._send)

    async def _send(self, content=None, ephemeral=False, **kw):
        self._done = True
        self.sent.append(NS(content=content, ephemeral=ephemeral, **kw))

    async def _defer(self, **kw):
        self._done = True

    async def _edit(self, **kw):
        self._done = True
        self.edits.append(kw)

    async def _modal(self, m):
        self._done, self.modal = True, m


class CogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = RpService(copy.deepcopy(CFG), RpStore(Path(self.tmp.name)))
        self.channel = FakeChannel(CH)
        self.now = [0.0]
        self.bot = NS(get_channel=lambda cid: self.channel if cid == CH else None)
        self.cog = RpDice(self.bot, self.svc, clock=lambda: self.now[0])
        await self.svc.set_roll_channel(G, CH, True, ADMIN)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def send(self, *a, **kw):
        m = message(*a, **kw)
        await self.cog.on_message(m)
        self.now[0] += 10
        return m.replies

    async def test_auto_roll_in_enabled_channel(self):
        with mock.patch.object(dice.secrets, "randbelow", lambda n: 98):
            r = await self.send("1d100>98")
        text, mention_author, allowed = r[0]
        self.assertEqual(text, "🎲 1d100 → 99 | ✅ 成功（99 > 98）")
        self.assertFalse(mention_author)
        self.assertFalse(allowed.everyone)

    async def test_auto_roll_scope(self):
        self.assertTrue(await self.send("2d6", channel=THREAD, parent=CH, mid=2))             # 继承父频道
        self.assertEqual(await self.send("2d6", channel=OTHER_THREAD, parent=CH2, mid=3), [])  # 无关子区
        self.assertEqual(await self.send("2d6", channel=CH2, mid=4), [])                      # 未启用频道
        self.assertEqual(await self.send("我掷 1d100>98 试试", mid=5), [])
        self.assertEqual(await self.send("99d6", mid=6), [])                                  # 超限静默
        self.assertEqual(await self.send("1d6", bot=True, mid=7), [])
        self.assertEqual(await self.send("1d6", webhook=55, mid=8), [])
        self.assertEqual(await self.send("1d6", mid=9, mtype=discord.MessageType.pins_add), [])

    async def test_auto_roll_dedupe_and_rate_limit(self):
        m = message("1d6", mid=42)
        await self.cog.on_message(m)
        self.now[0] += 10
        await self.cog.on_message(m)                                                         # 同一消息重复投递
        self.assertEqual(len(m.replies), 1)
        m2 = message("1d6", mid=43)
        await self.cog.on_message(m2)                                                        # 冷却已过：正常
        self.now[0] += 1
        m3 = message("1d6", mid=44)
        await self.cog.on_message(m3)                                                        # 同用户同频道 2 秒内
        self.assertEqual((len(m2.replies), m3.replies), (1, []))
        self.now[0] += 1.5
        m4 = message("1d6", mid=45)
        await self.cog.on_message(m4)
        self.assertEqual(len(m4.replies), 1)

    async def test_slash_roll_anywhere(self):
        it = FakeInteraction(uid=P1, channel=CH2)
        await self.cog.roll.callback(self.cog, it, expression="1d10>5")
        self.assertFalse(it.sent[0].ephemeral)
        self.assertIn("🎲 1d10 →", it.sent[0].content)
        it = FakeInteraction(uid=P2, channel=CH2)
        await self.cog.roll.callback(self.cog, it, expression="abc")
        self.assertTrue(it.sent[0].ephemeral)
        it = FakeInteraction(uid=P2, channel=CH2)
        await self.cog.roll.callback(self.cog, it, expression="1d5000")
        self.assertTrue(it.sent[0].ephemeral)
        self.assertIn("面数", it.sent[0].content)

    async def test_roll_config_admin_only(self):
        it = FakeInteraction(uid=RANDO)
        await self.cog.config_add.callback(self.cog, it, channel=NS(id=CH2, mention=f"<#{CH2}>"))
        self.assertIn("管理服务器", it.sent[0].content)
        it = FakeInteraction(uid=ADMIN, manage_guild=True)
        await self.cog.config_add.callback(self.cog, it, channel=NS(id=CH2, mention=f"<#{CH2}>"))
        self.assertTrue(self.svc.is_roll_channel(G, CH2, None))

    async def test_config_file_roll_channel(self):
        self.svc.cfg["guilds"] = {str(G): {"roll_channel_ids": [CH2]}}
        self.assertTrue(await self.send("1d6", channel=CH2, mid=60))                          # 配置文件里的频道
        self.assertTrue(await self.send("1d6", channel=OTHER_THREAD, parent=CH2, mid=61))     # 其子区继承
        it = FakeInteraction(uid=ADMIN, manage_guild=True)
        await self.cog.config_remove.callback(self.cog, it, channel=NS(id=CH2, mention=f"<#{CH2}>"))
        self.assertIn("配置文件", it.sent[0].content)
        self.assertTrue(self.svc.is_roll_channel(G, CH2, None))
        it = FakeInteraction(uid=ADMIN, manage_guild=True)
        await self.cog.config_list.callback(self.cog, it)
        self.assertIn(f"<#{CH2}>（配置文件，固定开启）", it.sent[0].content)
        self.assertIn(f"• <#{CH}>", it.sent[0].content)                                     # /roll-config 加的

    def test_real_config_channel(self):
        svc = RpService(load_config(), RpStore(Path(self.tmp.name) / "x"))
        self.assertTrue(svc.is_roll_channel(1541312608114581576, 1564068361711783976, None))

    async def test_start_permission(self):
        it = FakeInteraction(uid=RANDO)
        await self.cog.rp_start.callback(self.cog, it, progress_max=None)
        self.assertIn("可以开始探索", it.sent[0].content)
        self.svc.cfg["guilds"] = {str(G): {"dm_role_ids": [77]}}
        it = FakeInteraction(uid=DM, roles=(77,))
        await self.cog.rp_start.callback(self.cog, it, progress_max=8)
        self.assertFalse(it.sent[0].ephemeral)                                                # 公开开局消息
        self.assertIn("0/8", it.sent[0].content)
        self.assertTrue(it.sent[1].ephemeral)                                                 # DM 私密面板
        it2 = FakeInteraction(uid=DM, roles=(77,))
        await self.cog.rp_start.callback(self.cog, it2, progress_max=None)
        self.assertIn("已经有", it2.sent[0].content)

    async def start(self):
        it = FakeInteraction(uid=DM, manage_channels=True)
        await self.cog.rp_start.callback(self.cog, it, progress_max=None)
        sid = self.svc.require_active(G, CH)["id"]
        await self.svc.add_players(G, sid, Actor(DM), [P1])
        return sid

    async def test_dm_console_access(self):
        await self.start()
        it = FakeInteraction(uid=P1)
        await self.cog.rp_dm.callback(self.cog, it)
        self.assertTrue(it.sent[0].ephemeral)
        self.assertIn("只有本场 DM", it.sent[0].content)
        it = FakeInteraction(uid=ADMIN, administrator=True)
        await self.cog.rp_dm.callback(self.cog, it)
        self.assertIsNotNone(it.sent[-1].view)

    async def test_stat_change_announces_once(self):
        sid = await self.start()
        it = FakeInteraction(uid=DM, itype=discord.InteractionType.modal_submit)
        await self.cog.op_stats(it, sid, str(P1), {"sanity": "-15", "heat": "+20"})
        self.assertEqual(len(self.channel.sent), 1)                                           # 两项合并一条公告
        text, allowed = self.channel.sent[0]
        self.assertIn(f"📋 探索者状态更新 — <@{P1}>", text)
        self.assertIn("理智：100 → 85（-15）", text)
        self.assertIn("发情值：0 → 20（+20）", text)
        self.assertIn(f"操作者：<@{DM}>", text)
        self.assertFalse(allowed.everyone)
        self.assertEqual(self.svc.pending(G, sid), [])
        ev = self.svc.store.load(G)["sessions"][sid]["events"][-1]
        self.assertEqual(ev["announcement_message_id"], "901")
        it = FakeInteraction(uid=P1, itype=discord.InteractionType.modal_submit)               # 玩家伪造提交
        await self.cog.op_stats(it, sid, str(P1), {"sanity": "+50"})
        self.assertIn("只有本场 DM", it.sent[0].content)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_announcement_failure_is_retried_not_reapplied(self):
        sid = await self.start()
        self.channel.fail = True
        it = FakeInteraction(uid=DM, itype=discord.InteractionType.modal_submit)
        await self.cog.op_stats(it, sid, str(P1), {"sanity": "-10"})
        self.assertIn("公告发送失败", it.edits[-1]["embed"].description)
        self.assertEqual(self.svc.store.load(G)["sessions"][sid]["participants"][str(P1)]["sanity"], 90)
        self.channel.fail = False
        it = FakeInteraction(uid=DM)
        await self.cog.rp_dm.callback(self.cog, it)                                           # /rp dm 补发
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("理智：100 → 90（-10）", self.channel.sent[0][0])
        self.assertEqual(self.svc.store.load(G)["sessions"][sid]["participants"][str(P1)]["sanity"], 90)

    async def test_end_requires_confirmation(self):
        sid = await self.start()
        it = FakeInteraction(uid=DM)
        await self.cog.rp_end.callback(self.cog, it)
        self.assertEqual(self.svc.require_active(G, CH)["id"], sid)                           # 还没结束
        view = it.sent[-1].view
        yes = view.children[0]
        click = FakeInteraction(uid=DM, itype=discord.InteractionType.component)
        await yes.callback(click)
        self.assertIsNone(self.svc.active_session(G, CH))
        self.assertIn("🏁 探索结束", self.channel.sent[-1][0])
        it = FakeInteraction(uid=P1)
        await self.cog.rp_status.callback(self.cog, it)
        self.assertIn("没有进行中", it.sent[0].content)

    async def test_status_public(self):
        await self.start()
        it = FakeInteraction(uid=RANDO)
        await self.cog.rp_status.callback(self.cog, it)
        self.assertFalse(it.sent[0].ephemeral)
        fields = " ".join(f.value for e in it.sent[0].embeds for f in e.fields)
        self.assertIn(f"<@{P1}> 理智 **100** · 发情值 **0** · 异常：无", fields)


if __name__ == "__main__":
    unittest.main()

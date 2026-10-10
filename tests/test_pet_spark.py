"""撸猫续火花测试：python -m unittest discover tests"""
import asyncio
import json
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import discord

from minigames.pet_spark import lines as lines_mod
from minigames.pet_spark import renderer
from minigames.pet_spark.progress import (
    Progress, checkin, current_streak, level_for, parse_levels, should_remind, spark_active,
)
from minigames.pet_spark.service import PetSparkService, load_config

CFG = load_config()
LEVELS = parse_levels(CFG["levels"])
D = date(2026, 10, 1)


def day(n):
    return D + timedelta(days=n)


class ProgressTests(unittest.TestCase):
    def test_first_and_second_day_activates_spark(self):
        p = Progress(1, 1)
        r = checkin(p, day(0), LEVELS)
        self.assertTrue(r.counted and r.first_ever)
        self.assertEqual(p.streak_count, 1)
        self.assertFalse(spark_active(p, day(0)))
        r = checkin(p, day(1), LEVELS)
        self.assertTrue(r.spark_activated)
        self.assertEqual(p.streak_count, 2)
        self.assertTrue(spark_active(p, day(1)))

    def test_repeat_same_day_counts_once(self):
        p = Progress(1, 1)
        results = [checkin(p, day(0), LEVELS) for _ in range(100)]
        self.assertEqual(sum(r.counted for r in results), 1)
        self.assertEqual((p.streak_count, p.total_checkin_days), (1, 1))

    def test_break_resets_streak_keeps_best_total_level(self):
        p = Progress(1, 1)
        for i in range(30):
            checkin(p, day(i), LEVELS)
        self.assertEqual(level_for(p.total_checkin_days, LEVELS).name, "长明")
        r = checkin(p, day(31), LEVELS)  # 断了第 30 天
        self.assertTrue(r.comeback)
        self.assertEqual((p.streak_count, p.best_streak, p.total_checkin_days), (1, 30, 31))
        self.assertEqual(level_for(p.total_checkin_days, LEVELS).name, "长明")

    def test_derived_streak_when_not_checked_in(self):
        p = Progress(1, 1)
        checkin(p, day(0), LEVELS)
        checkin(p, day(1), LEVELS)
        self.assertEqual(current_streak(p, day(2)), 2)  # 昨天签过，今天还能续
        self.assertEqual(current_streak(p, day(3)), 0)  # 已断

    def test_should_remind(self):
        p = Progress(1, 1)
        checkin(p, day(0), LEVELS)
        self.assertFalse(should_remind(p, day(1)))       # 昨天只连续 1 天
        checkin(p, day(1), LEVELS)
        self.assertTrue(should_remind(p, day(2)))        # 昨天是连续第 2 天
        self.assertFalse(should_remind(p, day(1)))       # 今天已签
        self.assertFalse(should_remind(p, day(3)))       # 前天以前最后签到 = 已断
        p.reminder_opt_out = True
        self.assertFalse(should_remind(p, day(2)))

    def test_no_reminder_until_streak_rebuilt(self):
        p = Progress(1, 1)
        for i in range(5):
            checkin(p, day(i), LEVELS)
        checkin(p, day(7), LEVELS)                        # 断签后第一天
        self.assertFalse(should_remind(p, day(8)))
        checkin(p, day(8), LEVELS)                        # 重新连续 2 天
        self.assertTrue(should_remind(p, day(9)))

    def test_levels(self):
        self.assertIsNone(level_for(0, LEVELS))
        self.assertEqual([level_for(n, LEVELS).name for n in (1, 2, 6, 7, 14, 30, 100, 500)],
                         ["初识", "微光", "微光", "暖焰", "炽焰", "长明", "不灭", "不灭"])


class LinesTests(unittest.TestCase):
    def test_empty_or_disabled_pools_fallback(self):
        rng = random.Random(1)
        self.assertEqual(lines_mod.pick({"groups": {}}, "meme", 0, rng), lines_mod.DEFAULT_FALLBACK)
        lines = {"fallback": "喵?", "groups": {"meme": {"a": {"enabled": False, "entries": ["x"]},
                                                        "b": {"entries": []}}}}
        self.assertEqual(lines_mod.pick(lines, "meme", 0, rng), "喵?")

    def test_min_level_gate(self):
        lines = {"groups": {"g": {"vip": {"min_level": 4, "entries": ["vip"]}, "all": {"entries": ["all"]}}}}
        rng = random.Random(1)
        self.assertEqual({lines_mod.pick(lines, "g", 3, rng) for _ in range(50)}, {"all"})
        self.assertIn("vip", {lines_mod.pick(lines, "g", 4, rng) for _ in range(50)})

    def test_render_only_whitelisted_placeholders(self):
        self.assertEqual(lines_mod.render("{mention} {foo} {streak}", mention="<@1>", streak=3), "<@1> {foo} 3")

    def test_bundled_lines_and_song_reuse(self):
        lines = lines_mod.load_lines()
        songs = lines["groups"]["meme"]["songs"]["entries"]
        from responses import CHAT_RESPONSES
        self.assertTrue(set(CHAT_RESPONSES["玉猫唱歌"]) <= set(songs))

    def test_checkin_message_mentions_no_user_input(self):
        p = Progress(1, 42)
        r = checkin(p, day(0), LEVELS)
        msg = renderer.checkin_message(42, p, r, day(0), LEVELS, lines_mod.load_lines(), random.Random(1))
        self.assertTrue(msg.startswith("<@42> "))
        self.assertIn("今日撸猫成功", msg)
        self.assertEqual(msg.count("<@"), 1)


class MilestoneTests(unittest.TestCase):
    MILESTONES = (7, 14, 30, 100, 365)

    def setUp(self):
        self.lines = lines_mod.load_lines()
        self.rng = random.Random(3)

    def msg(self, p, r, today):
        return renderer.checkin_message(1, p, r, today, LEVELS, self.lines, self.rng)

    def test_configured_days(self):
        self.assertEqual(lines_mod.milestone_days(self.lines), set(self.MILESTONES))

    def test_fires_exactly_on_milestone_days(self):
        p = Progress(1, 1)
        fired = []
        for i in range(400):
            r = checkin(p, day(i), LEVELS)
            text = self.msg(p, r, day(i))
            if "🏆" in text:
                fired.append(p.streak_count)
                entries = self.lines["groups"]["milestone"][str(p.streak_count)]["entries"]
                self.assertTrue(any(e in text for e in entries))
                self.assertIn("今日撸猫成功", text)  # 正常回应仍保留
        self.assertEqual(fired, list(self.MILESTONES))

    def test_repeat_same_day_no_milestone(self):
        p = Progress(1, 1)
        for i in range(7):
            r = checkin(p, day(i), LEVELS)
        self.assertIn("🏆", self.msg(p, r, day(6)))
        for _ in range(5):
            r = checkin(p, day(6), LEVELS)
            self.assertNotIn("🏆", self.msg(p, r, day(6)))

    def test_retriggers_after_break(self):
        p = Progress(1, 1)
        fired = []
        for i in list(range(7)) + list(range(10, 17)):  # 第 7 天达成后断签，再连续 7 天
            r = checkin(p, day(i), LEVELS)
            if "🏆" in self.msg(p, r, day(i)):
                fired.append(i)
        self.assertEqual(fired, [6, 16])


class MemeDistributionTests(unittest.TestCase):
    def setUp(self):
        self.lines = lines_mod.load_lines()
        self.index = {e: k for k, v in self.lines["groups"]["meme"].items() for e in v["entries"]}

    def share(self, level, n=200000):
        rng = random.Random(11)
        counts = {}
        for _ in range(n):
            k = self.index[lines_mod.pick(self.lines, "meme", level, rng)]
            counts[k] = counts.get(k, 0) + 1
        return {k: c / n for k, c in counts.items()}

    def test_anomaly_about_one_percent_at_any_level(self):
        for level in (0, 4, 6):
            self.assertAlmostEqual(self.share(level)["anomaly"], 0.01, delta=0.002, msg=level)

    def test_close_friend_gated_by_level(self):
        self.assertNotIn("close_friend", self.share(3, n=20000))
        rng = random.Random(5)
        seen = {lines_mod.pick(self.lines, "meme", 4, rng) for _ in range(20000)}
        new = "你来了？正好，帮本喵把那个杯子推下去。"
        self.assertIn(new, seen)

    def test_all_meme_pools_reachable(self):
        self.assertEqual(set(self.share(4, n=50000)), set(self.lines["groups"]["meme"]))

    def test_songs_include_still_works(self):
        from responses import CHAT_RESPONSES
        songs = self.lines["groups"]["meme"]["songs"]
        self.assertEqual(songs["include_chat_responses"], "玉猫唱歌")
        self.assertTrue(set(CHAT_RESPONSES["玉猫唱歌"]) <= set(songs["entries"]))

    def test_other_groups_unaffected(self):
        rng = random.Random(1)
        reminder = {lines_mod.pick(self.lines, "reminder", 6, rng) for _ in range(500)}
        self.assertTrue(all("{mention}" in r for r in reminder))
        comeback = set(sum((p["entries"] for p in self.lines["groups"]["comeback"].values()), []))
        self.assertTrue({lines_mod.pick(self.lines, "comeback", 6, rng) for _ in range(200)} <= comeback)


# ---------------------------------------------------------------- 服务层

class Clock:
    def __init__(self, dt):
        self.now = dt

    def __call__(self):
        return self.now

    def set_local(self, y, m, d, hh, mm, tz_hours=8):
        self.now = datetime(y, m, d, hh, mm, tzinfo=timezone(timedelta(hours=tz_hours))).astimezone(timezone.utc)


class FakeChannel:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, content, allowed_mentions=None):
        if self.fail:
            raise discord.Forbidden(NS(status=403, reason="no"), "no perms")
        self.sent.append((content, allowed_mentions))


class FakeGuild:
    def __init__(self, gid, members):
        self.id, self.members = gid, set(members)

    def get_member(self, uid):
        return object() if uid in self.members else None

    async def fetch_member(self, uid):
        raise discord.NotFound(NS(status=404, reason="gone"), "gone")


class FakeBot:
    def __init__(self):
        self.channels, self.guilds = {}, {}

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        raise discord.NotFound(NS(status=404, reason="gone"), "gone")

    def get_guild(self, gid):
        return self.guilds.get(gid)


G1, G2 = 111, 222
CH1, CH2 = 1001, 2002


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = json.loads(json.dumps(CFG))
        self.cfg["state_file"] = str(Path(self.tmp.name) / "state.json")
        self.cfg["guilds"] = {
            str(G1): {"timezone": "Asia/Shanghai", "reminder_enabled": True, "reminder_channel_id": CH1},
            str(G2): {"timezone": "America/New_York", "reminder_enabled": True, "reminder_channel_id": CH2},
        }
        self.clock = Clock(None)
        self.bot = FakeBot()
        self.ch1, self.ch2 = FakeChannel(), FakeChannel()
        self.bot.channels = {CH1: self.ch1, CH2: self.ch2}
        self.bot.guilds = {G1: FakeGuild(G1, range(1, 100)), G2: FakeGuild(G2, range(1, 100))}
        self.svc = self.new_service()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def new_service(self):
        return PetSparkService(self.bot, self.cfg, rng=random.Random(1), clock=self.clock)

    async def checkin_on(self, uid, local_day, guild=G1, hh=12):
        self.clock.set_local(2026, 10, local_day, hh, 0)
        return await self.svc.checkin(guild, uid)

    async def tick_at(self, local_day, hh, mm, svc=None):
        self.clock.set_local(2026, 10, local_day, hh, mm)
        await (svc or self.svc).reminder_tick()

    def mentioned(self, channel):
        return [m for content, _ in channel.sent for m in content.split() if m.startswith("<@")]

    async def test_reminder_window_once_per_day(self):
        await self.checkin_on(1, 1)
        await self.checkin_on(1, 2)
        await self.tick_at(3, 21, 59)
        self.assertEqual(self.ch1.sent, [])
        await self.tick_at(3, 22, 0)
        self.assertEqual(self.mentioned(self.ch1), ["<@1>"])
        await self.tick_at(3, 22, 30)                       # 定时器重跑
        self.svc = self.new_service()                       # Bot 重启
        await self.tick_at(3, 23, 0)
        self.assertEqual(len(self.ch1.sent), 1)
        # 22:01 后签到仍算今天，火花保住
        p, r, _ = await self.checkin_on(1, 3, hh=23)
        self.assertTrue(r.counted)
        self.assertEqual(p.streak_count, 3)

    async def test_crash_after_reserve_does_not_double_mention(self):
        await self.checkin_on(1, 1)
        await self.checkin_on(1, 2)
        self.ch1.fail = True
        await self.tick_at(3, 22, 0)
        self.ch1.fail = False
        self.svc = self.new_service()
        self.svc.reminder_runs.clear()                      # 即使"已检查"标记丢失
        await self.tick_at(3, 22, 5)
        self.assertEqual(self.ch1.sent, [])                 # 预约记录挡住了重复 @

    async def test_non_eligible_not_reminded(self):
        await self.checkin_on(1, 3)                         # 用户 1：昨天只连续 1 天
        for d in (1, 2):                                    # 用户 2：前天以前最后签到，已断
            await self.checkin_on(2, d)
        for d in (2, 3, 4):                                 # 用户 3：火花有效，但今天 22:00 前已签
            await self.checkin_on(3, d)
        await self.tick_at(4, 22, 0)
        self.assertEqual(self.mentioned(self.ch1), [])

    async def test_opt_out_and_member_left(self):
        for uid in (1, 2, 500):
            await self.checkin_on(uid, 1)
            await self.checkin_on(uid, 2)
        await self.svc.set_opt_out(G1, 2, True)
        await self.tick_at(3, 22, 0)
        self.assertEqual(self.mentioned(self.ch1), ["<@1>"])  # 2 关闭催撸，500 已退服

    async def test_guild_isolation_and_timezones(self):
        await self.checkin_on(1, 1, guild=G1)
        await self.checkin_on(1, 2, guild=G1)
        # 同一用户在 G2 只签了一天
        await self.checkin_on(1, 2, guild=G2)
        self.assertEqual(self.svc.progress[f"{G2}:1"].streak_count, 1)
        # 上海 10/3 22:00 = 纽约 10/3 10:00：G1 催，G2 不催
        await self.tick_at(3, 22, 0)
        self.assertEqual(len(self.ch1.sent), 1)
        self.assertEqual(self.ch2.sent, [])

    async def test_local_date_boundary(self):
        # 上海 10/2 23:59 签到算 10/2，00:01 算 10/3
        p, r, today = await self.checkin_on(1, 2, hh=23)
        self.assertEqual(today, date(2026, 10, 2))
        self.clock.set_local(2026, 10, 3, 0, 1)
        p, r, today = await self.svc.checkin(G1, 1)
        self.assertEqual((today, r.counted, p.streak_count), (date(2026, 10, 3), True, 2))

    async def test_concurrent_pets_milestone_once(self):
        for d in range(1, 7):
            await self.checkin_on(9, d)
        self.clock.set_local(2026, 10, 7, 12, 0)

        async def pet():
            p, r, today = await self.svc.checkin(G1, 9)
            return self.svc.checkin_message(9, p, r, today)
        messages = await asyncio.gather(*(pet() for _ in range(20)))
        self.assertEqual(sum("🏆" in m for m in messages), 1)
        self.assertEqual(self.svc.progress[f"{G1}:9"].streak_count, 7)

    async def test_concurrent_pets_count_once(self):
        self.clock.set_local(2026, 10, 1, 12, 0)
        results = await asyncio.gather(*(self.svc.checkin(G1, 7) for _ in range(20)))
        self.assertEqual(sum(r.counted for _, r, _ in results), 1)
        self.assertEqual(self.svc.progress[f"{G1}:7"].total_checkin_days, 1)

    async def test_missing_channel_or_forbidden_is_safe(self):
        await self.checkin_on(1, 1)
        await self.checkin_on(1, 2)
        self.bot.channels.pop(CH1)
        await self.tick_at(3, 22, 0)                        # 频道不存在：跳过不崩溃
        self.assertEqual(self.svc.reminder_runs[str(G1)], "2026-10-03")

    async def test_batches_merge_mentions(self):
        self.cfg["reminder_mentions_per_message"] = 4
        self.svc = self.new_service()
        for uid in range(1, 11):
            await self.checkin_on(uid, 1)
            await self.checkin_on(uid, 2)
        await self.tick_at(3, 22, 0)
        self.assertEqual(len(self.ch1.sent), 3)             # 10 人分 3 条
        self.assertEqual(sorted(self.mentioned(self.ch1)), sorted(f"<@{u}>" for u in range(1, 11)))
        for content, allowed in self.ch1.sent:
            self.assertFalse(allowed.everyone)
            self.assertTrue(all(f"<@{o.id}>" in content for o in allowed.users))

    async def test_progress_persists(self):
        await self.checkin_on(5, 1)
        await self.checkin_on(5, 2)
        svc2 = self.new_service()
        self.assertEqual(svc2.progress[f"{G1}:5"].streak_count, 2)

    async def test_corrupt_state_is_backed_up(self):
        Path(self.cfg["state_file"]).write_text("{not json", encoding="utf-8")
        self.clock.set_local(2026, 10, 1, 12, 0)
        svc = self.new_service()
        self.assertEqual(svc.progress, {})
        self.assertTrue(list(Path(self.tmp.name).glob("state.corrupt-*.json")))


if __name__ == "__main__":
    unittest.main()

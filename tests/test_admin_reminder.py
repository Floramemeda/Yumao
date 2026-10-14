"""管理计时提醒测试：python -m unittest discover tests"""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import discord

from minigames.admin_reminder.cog import LIST_LIMIT, list_embed
from minigames.admin_reminder.service import ReminderService, load_config

CFG = load_config()
SH = timezone(timedelta(hours=8))


class Clock:
    def __init__(self):
        self.now = None

    def __call__(self):
        return self.now

    def set(self, y, m, d, hh, mm=0):
        self.now = datetime(y, m, d, hh, mm, tzinfo=SH).astimezone(timezone.utc)


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def send(self, content, allowed_mentions=None):
        if self.fail:
            raise discord.HTTPException(NS(status=500, reason="x"), "x")
        self.sent.append((content, allowed_mentions))


class FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, cid):
        return self.channel if cid == CFG["notify_channel_id"] else None

    async def fetch_channel(self, cid):
        raise discord.NotFound(NS(status=404, reason="nf"), "nf")


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dict(CFG, state_file=str(Path(self.tmp.name) / "r.json"))
        self.clock = Clock()
        self.channel = FakeChannel()
        self.svc = self.new_service()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def new_service(self):
        return ReminderService(FakeBot(self.channel), self.cfg, clock=self.clock)

    async def tick_at(self, *when):
        self.clock.set(*when)
        await self.svc.tick()

    def test_role_check(self):
        role = NS(id=CFG["role_id"])
        self.assertTrue(self.svc.is_manager(NS(roles=[NS(id=1), role])))
        self.assertFalse(self.svc.is_manager(NS(roles=[NS(id=1)])))
        self.assertFalse(self.svc.is_manager(NS()))   # 非成员对象（如私信）

    async def test_notifies_day_before_at_configured_time(self):
        self.clock.set(2026, 10, 14, 15, 0)
        r = await self.svc.create(1, 42, "续费服务器", 5)
        self.assertEqual(r["due_date"], "2026-10-19")
        await self.tick_at(2026, 10, 18, 9, 59)
        self.assertEqual(self.channel.sent, [])
        await self.tick_at(2026, 10, 18, 10, 0)
        self.assertEqual(len(self.channel.sent), 1)
        content, am = self.channel.sent[0]
        self.assertTrue(content.startswith("<@42>，续费服务器在明天到期！"))
        self.assertEqual(([u.id for u in am.users], am.everyone, am.roles), ([42], False, False))
        await self.tick_at(2026, 10, 18, 12, 0)            # 不重复发送
        self.assertEqual(len(self.channel.sent), 1)

    async def test_one_day_after_notify_time_sends_now(self):
        self.clock.set(2026, 10, 14, 15, 0)
        r = await self.svc.create(1, 42, "活动报名", 1)
        self.assertEqual(r["due_date"], "2026-10-15")
        await self.svc.tick()
        self.assertIn("在明天到期", self.channel.sent[0][0])

    async def test_one_day_before_notify_time_waits(self):
        self.clock.set(2026, 10, 14, 8, 0)
        await self.svc.create(1, 42, "活动报名", 1)
        await self.svc.tick()
        self.assertEqual(self.channel.sent, [])
        await self.tick_at(2026, 10, 14, 10, 0)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_downtime_catch_up_and_overdue_wording(self):
        self.clock.set(2026, 10, 14, 15, 0)
        await self.svc.create(1, 42, "A", 3)               # 到期 10/17，提醒 10/16 10:00
        await self.svc.create(1, 43, "B", 2)               # 到期 10/16，提醒 10/15 10:00
        self.svc = self.new_service()                      # 重启
        await self.tick_at(2026, 10, 16, 20, 0)            # 停机到 10/16 晚上才恢复
        texts = [c for c, _ in self.channel.sent]
        self.assertEqual(len(texts), 2)
        self.assertTrue(any(t.startswith("<@43>，B已于 10 月 16 日到期！") for t in texts))
        self.assertTrue(any(t.startswith("<@42>，A在明天到期！") for t in texts))

    async def test_send_failure_retries_later(self):
        self.clock.set(2026, 10, 14, 15, 0)
        await self.svc.create(1, 42, "X", 1)
        self.channel.fail = True
        await self.svc.tick()
        self.channel.fail = False
        await self.tick_at(2026, 10, 14, 15, 1)
        self.assertEqual(len(self.channel.sent), 1)

    async def test_mentions_in_item_do_not_ping(self):
        self.clock.set(2026, 10, 14, 15, 0)
        await self.svc.create(1, 42, "@everyone <@&123> <@99> 交材料", 1)
        await self.svc.tick()
        content, am = self.channel.sent[0]
        self.assertIn("@everyone", content)                 # 原样显示
        self.assertEqual(([u.id for u in am.users], am.everyone, am.roles), ([42], False, False))

    async def test_persisted_and_pruned(self):
        self.clock.set(2026, 10, 14, 15, 0)
        await self.svc.create(1, 42, "X", 3)
        data = json.loads(Path(self.cfg["state_file"]).read_text(encoding="utf-8"))
        self.assertEqual(len(data["reminders"]), 1)
        await self.tick_at(2026, 10, 16, 10, 0)
        self.clock.set(2026, 12, 1, 10, 0)
        await self.svc.create(1, 42, "Y", 100)             # 创建时清理：已发送超过 30 天的被移除
        items = [r["item"] for r in self.svc.reminders.values()]
        self.assertEqual(items, ["Y"])

    async def test_pending_list(self):
        self.clock.set(2026, 10, 14, 15, 0)
        await self.svc.create(1, 42, "晚到期", 10)
        await self.svc.create(1, 43, "早到期", 3)
        await self.svc.create(1, 44, "已发送", 1)
        await self.svc.create(2, 45, "别的服务器", 2)
        await self.svc.tick()                               # "已发送" 发出
        rows = self.svc.pending(1)
        self.assertEqual([r["item"] for r in rows], ["早到期", "晚到期"])
        text = list_embed(rows).description
        self.assertLess(text.index("早到期"), text.index("晚到期"))
        self.assertIn("<@43>", text)
        self.assertNotIn("已发送", text)
        self.assertNotIn("别的服务器", text)

    async def test_list_empty_truncated_and_escaped(self):
        self.assertIn("没有待发送", list_embed([]).description)
        self.clock.set(2026, 10, 14, 15, 0)
        for i in range(LIST_LIMIT + 5):
            await self.svc.create(1, 42, f"项目{i}", 5)
        await self.svc.create(1, 42, "**粗体** _斜体_", 6)
        embed = list_embed(self.svc.pending(1))
        self.assertIn("以及其他 6 条", embed.description)
        self.assertLessEqual(len(embed.description), 4096)
        self.assertIn("共 26 条", embed.footer.text)
        self.assertIn(r"\*\*粗体\*\*", list_embed([r for r in self.svc.pending(1) if "粗体" in r["item"]]).description)

    async def test_dst_free_local_dates(self):
        # 本地午夜前后创建：按北京时间日期计算到期日
        self.clock.set(2026, 10, 14, 23, 59)
        r = await self.svc.create(1, 42, "X", 1)
        self.assertEqual(r["due_date"], "2026-10-15")
        self.clock.set(2026, 10, 15, 0, 1)
        r = await self.svc.create(1, 42, "Y", 1)
        self.assertEqual(r["due_date"], "2026-10-16")


if __name__ == "__main__":
    unittest.main()

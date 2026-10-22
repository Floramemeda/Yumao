"""喵咪日事本测试：python -m unittest discover tests"""
import asyncio
import copy
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import discord

from minigames.cat_diary import store as st
from minigames.cat_diary.cog import (
    MSG_BAD_MENTION, MSG_NOTHING, CatDiary, load_config, render_results,
)
from minigames.cat_diary.store import DiaryStore
from minigames.cat_diary.text import parse_write, search_key

BOT = 777
G1, G2 = 1001, 1002
CH1, CH2 = 2001, 2002
BASE = datetime(2026, 10, 22, 1, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = BASE

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def make_cfg(tmp):
    cfg = copy.deepcopy(load_config())
    cfg["guilds"] = {str(G1): {"enabled": True, "write_channel_id": CH1},
                     str(G2): {"enabled": True, "write_channel_id": CH2}}
    cfg["data_dir"] = str(Path(tmp) / "cat_diary")
    return cfg


# ---------------------------------------------------------------- 解析

class ParseTests(unittest.TestCase):
    def test_trigger_variants(self):
        for text in (f"记笔记 <@{BOT}> 今天吃薯饼", f"記筆記 <@!{BOT}> 今天吃薯餅", f"記笔記 <@{BOT}> 今天吃薯饼",
                     f"  记笔记　<@{BOT}>\n多行\n正文  "):
            with self.subTest(text):
                p = parse_write(text, BOT)
                self.assertIsNotNone(p)
                self.assertFalse(p.bad_mention)
        self.assertEqual(parse_write(f"記筆記 <@{BOT}> 今天吃薯餅", BOT).body, "今天吃薯餅")  # 正文不转换

    def test_ignored(self):
        for text in (f"今天记笔记 <@{BOT}> 内容", "记笔记 @造化玉猫 内容", f"记笔记 <@123> 内容",
                     f"记笔记<@{BOT}> 内容", "记笔记", f"<@{BOT}> 记笔记 内容"):
            with self.subTest(text):
                self.assertIsNone(parse_write(text, BOT))

    def test_body_mentions(self):
        bad = ["<@123>", "<@!123>", f"<@{BOT}>", "<@&55>", "<#66>", "@everyone", "@here", "大家@everyone你好", "@Everyone"]
        for b in bad:
            with self.subTest(b):
                self.assertTrue(parse_write(f"记笔记 <@{BOT}> 内容 {b}", BOT).bad_mention)
        for ok in ("mail me: cat@example.com", "@造化玉猫 是只猫", "我@了一下"):
            with self.subTest(ok):
                self.assertFalse(parse_write(f"记笔记 <@{BOT}> {ok}", BOT).bad_mention)

    def test_search_key(self):
        self.assertEqual(search_key("薯餅ABC"), search_key("薯饼abc"))
        self.assertIn(search_key("薯餅"), search_key("今天在媚笙阁吃了薯饼"))


# ---------------------------------------------------------------- Discord 层伪造

def make_message(content, author=1, guild=G1, channel=CH1, mid=1, created=BASE, bot=False, webhook=None):
    ids = [int(x) for x in re.findall(r"<@!?(\d+)>", content)]
    return NS(
        content=content, id=mid, created_at=created, webhook_id=webhook,
        guild=NS(id=guild), channel=NS(id=channel), author=NS(id=author, bot=bot),
        mentions=[NS(id=i) for i in dict.fromkeys(ids)],
        role_mentions=re.findall(r"<@&(\d+)>", content),
        channel_mentions=re.findall(r"<#(\d+)>", content),
        mention_everyone=("@everyone" in content or "@here" in content),
        replies=[],
    )


async def _reply(self, text, mention_author=True, allowed_mentions=None):
    self.replies.append((text, allowed_mentions))


class FakeInteraction:
    def __init__(self, uid=1, guild=G1, members=None):
        self.user = NS(id=uid)
        self.guild_id = guild
        self.guild = NS(get_member=lambda i: NS(display_name=f"成员{i}") if i in (members or set()) else None)
        self.deferred = None
        self.sent = []
        self.response = NS(defer=self._defer)
        self.followup = NS(send=self._send)

    async def _defer(self, **kw):
        self.deferred = kw

    async def _send(self, content=None, embeds=None, allowed_mentions=None, **kw):
        self.sent.append(NS(content=content, embeds=embeds or [], allowed=allowed_mentions, extra=kw))


class FakeBot:
    def __init__(self):
        self.user = NS(id=BOT)

    def get_user(self, uid):
        return None

    async def fetch_user(self, uid):
        if uid == 404:
            raise discord.NotFound(NS(status=404, reason="nf"), "nf")
        return NS(display_name=f"旧成员{uid}")


class DiaryTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        self.clock = Clock()
        self.cog = self.new_cog()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def new_cog(self):
        return CatDiary(FakeBot(), self.cfg, DiaryStore(self.cfg, clock=self.clock))

    async def write(self, content, **kw):
        msg = make_message(content, **kw)
        msg.reply = _reply.__get__(msg)
        await self.cog.on_message(msg)
        return msg.replies

    async def note(self, body, author=1, mid=None, guild=G1, channel=None, created=None):
        mid = mid or (len(self.entries(guild)) + 1) * 10 + author
        r = await self.write(f"记笔记 <@{BOT}> {body}", author=author, mid=mid, guild=guild,
                             channel=channel or (CH1 if guild == G1 else CH2), created=created or self.clock())
        self.clock.advance(self.cfg["write_cooldown_seconds"])
        return r

    async def read(self, user=None, text=None, uid=1, guild=G1, members=None):
        it = FakeInteraction(uid, guild, members)
        await self.cog.read_diary.callback(self.cog, it, user=NS(id=user) if user else None, text=text)
        return it

    def entries(self, guild=G1):
        p = Path(self.cfg["data_dir"]) / f"{guild}.json"
        return json.loads(p.read_text(encoding="utf-8"))["entries"] if p.exists() else []


# ---------------------------------------------------------------- 写入

class WriteTests(DiaryTestBase):
    async def test_success_exact_reply_and_mentions(self):
        replies = await self.write(f"記筆記 <@!{BOT}> 今天吃薯餅", author=42, mid=5)
        text, allowed = replies[0]
        self.assertEqual(text, "<@42> 已经帮你记入在喵咪日事上了！你放心！本喵的日事本不会被别人看！！")
        self.assertEqual(([u.id for u in allowed.users], allowed.everyone, allowed.roles), ([42], False, False))
        e = self.entries()[0]
        self.assertEqual((e["user_id"], e["message_id"], e["channel_id"], e["content"]), ("42", "5", str(CH1), "今天吃薯餅"))
        self.assertEqual(e["created_at"], "2026-10-22T01:00:00Z")

    async def test_ignored_sources(self):
        cases = [
            dict(content=f"记笔记 <@{BOT}> x", channel=9999),         # 非指定频道
            dict(content=f"记笔记 <@{BOT}> x", bot=True),             # 机器人
            dict(content=f"记笔记 <@{BOT}> x", webhook=5),            # webhook
            dict(content=f"今天记笔记 <@{BOT}> x"),
            dict(content="记笔记 @造化玉猫 x"),                        # 伪造纯文字 @
            dict(content="记笔记 <@555> x"),                          # 其他 bot/用户
            dict(content=f"记笔记 <@{BOT}> x", guild=3003),           # 未配置的服务器
        ]
        for kw in cases:
            with self.subTest(kw):
                self.assertEqual(await self.write(**kw), [])
        self.assertEqual(self.entries(), [])

    async def test_rejections_do_not_consume_cooldown(self):
        r = await self.write(f"记笔记 <@{BOT}>   ", mid=1)
        self.assertIn("要记什么", r[0][0])
        r = await self.write(f"记笔记 <@{BOT}> " + "喵" * 1001, mid=2)
        self.assertIn("最多记 1000", r[0][0])
        for i, bad in enumerate(["<@9>", "<@&9>", "<#9>", "@everyone", "@here", f"<@{BOT}>"]):
            r = await self.write(f"记笔记 <@{BOT}> 看 {bad}", mid=10 + i)
            self.assertEqual(r[0][0], MSG_BAD_MENTION)
            self.assertEqual(r[0][1].everyone, False)
        r = await self.write(f"记笔记 <@{BOT}> " + "喵" * 1000, mid=99)   # 刚好 1000 字，且没有被扣冷却
        self.assertIn("已经帮你记入", r[0][0])
        self.assertEqual(len(self.entries()), 1)

    async def test_write_cooldown(self):
        await self.write(f"记笔记 <@{BOT}> 一", mid=1)
        self.clock.advance(299.2)
        r = await self.write(f"记笔记 <@{BOT}> 二", mid=2)
        self.assertIn("1 秒后", r[0][0])                                   # ceil，至少 1 秒
        self.clock.advance(0.8)
        r = await self.write(f"记笔记 <@{BOT}> 二", mid=3)
        self.assertIn("已经帮你记入", r[0][0])

    async def test_duplicate_message_id(self):
        await self.write(f"记笔记 <@{BOT}> 一", mid=1)
        before = json.loads((Path(self.cfg["data_dir"]) / f"{G1}.json").read_text(encoding="utf-8"))
        self.clock.advance(400)
        r = await self.write(f"记笔记 <@{BOT}> 一", mid=1)                   # 重放
        self.assertEqual(r, [])
        after = json.loads((Path(self.cfg["data_dir"]) / f"{G1}.json").read_text(encoding="utf-8"))
        self.assertEqual(before, after)                                      # 不入库、不刷新冷却

    async def test_no_delete_or_edit_sync(self):
        for name in ("on_message_delete", "on_raw_message_delete", "on_message_edit", "on_raw_message_edit"):
            self.assertFalse(any(n == name for n, _ in self.cog.get_listeners()))

    async def test_concurrent_same_user_one_write(self):
        msgs = []
        for i in range(10):
            m = make_message(f"记笔记 <@{BOT}> 并发{i}", mid=100 + i)
            m.reply = _reply.__get__(m)
            msgs.append(m)
        await asyncio.gather(*(self.cog.on_message(m) for m in msgs))
        self.assertEqual(len(self.entries()), 1)

    async def test_capacity_exact_under_concurrency(self):
        self.cfg["max_entries_per_guild"] = 5
        msgs = []
        for i in range(12):
            m = make_message(f"记笔记 <@{BOT}> 抢{i}", author=100 + i, mid=500 + i)
            m.reply = _reply.__get__(m)
            msgs.append(m)
        await asyncio.gather(*(self.cog.on_message(m) for m in msgs))
        self.assertEqual(len(self.entries()), 5)
        full = [m for m in msgs if m.replies and "写满" in m.replies[0][0]]
        self.assertEqual(len(full), 7)

    async def test_storage_failure_is_atomic(self):
        await self.note("第一条")
        path = Path(self.cfg["data_dir"]) / f"{G1}.json"
        before = path.read_text(encoding="utf-8")
        with mock.patch("minigames.cat_diary.store.os.replace", side_effect=OSError("disk")):
            r = await self.write(f"记笔记 <@{BOT}> 第二条", author=2, mid=900)
        self.assertIn("没记上", r[0][0])
        self.assertNotIn("已经帮你记入", r[0][0])
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(len(self.cog.store.docs[G1]["entries"]), 1)          # 内存也没变
        self.assertFalse(list(path.parent.glob("*.tmp")))
        r = await self.write(f"记笔记 <@{BOT}> 第二条", author=2, mid=900)   # 没扣冷却，可立即重试
        self.assertIn("已经帮你记入", r[0][0])

    async def test_corrupt_file_not_overwritten(self):
        path = Path(self.cfg["data_dir"]) / f"{G1}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        r = await self.write(f"记笔记 <@{BOT}> x", mid=1)
        self.assertIn("没记上", r[0][0])
        it = await self.read()
        self.assertIn("出了点问题", it.sent[0].content)
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


# ---------------------------------------------------------------- 查询

class ReadTests(DiaryTestBase):
    async def seed(self):
        await self.note("今天吃了薯餅", author=1)
        await self.note("ABC 大写", author=2)
        await self.note("今天吃了薯饼第二次", author=2)
        await self.note("别的事", author=1)

    def contents(self, it):
        return [e.description for m in it.sent for e in m.embeds]

    async def test_four_combinations(self):
        await self.seed()
        self.assertEqual(self.contents(await self.read(uid=10)), ["别的事", "今天吃了薯饼第二次", "ABC 大写", "今天吃了薯餅"])
        self.assertEqual(self.contents(await self.read(user=2, uid=11)), ["今天吃了薯饼第二次", "ABC 大写"])
        self.assertEqual(self.contents(await self.read(text="薯饼", uid=12)), ["今天吃了薯饼第二次", "今天吃了薯餅"])
        self.assertEqual(self.contents(await self.read(user=1, text="薯餅", uid=13)), ["今天吃了薯餅"])
        self.assertEqual(self.contents(await self.read(text="abc", uid=14)), ["ABC 大写"])

    async def test_latest_five_and_tie_breaker(self):
        same = BASE + timedelta(hours=1)
        for i in range(7):
            await self.note(f"第{i}条", author=1, mid=1000 + i, created=same)
        it = await self.read(uid=20)
        self.assertEqual(self.contents(it), [f"第{i}条" for i in (6, 5, 4, 3, 2)])

    async def test_empty_and_public(self):
        it = await self.read()
        self.assertEqual(it.sent[0].content, MSG_NOTHING)
        self.assertNotEqual(it.deferred.get("ephemeral"), True)
        it2 = await self.read()                                                # 冷却提示也公开
        self.assertIn("秒后再来", it2.sent[0].content)
        it3 = await self.read(text="   ", uid=30)
        self.assertIn("不能只有空格", it3.sent[0].content)
        it4 = await self.read(text="喵" * 101, uid=31)
        self.assertIn("最多 100", it4.sent[0].content)
        for i in (it, it2, it3, it4):
            self.assertNotEqual(i.deferred.get("ephemeral"), True)
            self.assertTrue(all(m.extra.get("ephemeral") is not True for m in i.sent))
            self.assertTrue(all(m.allowed.everyone is False and m.allowed.users is False for m in i.sent))

    async def test_read_cooldown_rules(self):
        await self.read(text="  ", uid=5)                                      # 参数错误不扣冷却
        it = await self.read(text="不存在", uid=5)                              # 零结果也扣冷却
        self.assertEqual(it.sent[0].content, MSG_NOTHING)
        self.clock.advance(59.5)
        self.assertIn("1 秒后", (await self.read(uid=5)).sent[0].content)
        self.assertEqual((await self.read(uid=6)).sent[0].content, MSG_NOTHING)  # 别人不受影响
        self.clock.advance(0.5)
        self.assertEqual((await self.read(uid=5)).sent[0].content, MSG_NOTHING)

    async def test_user_header_and_departed_user(self):
        await self.note("我走了但笔记还在", author=404)
        await self.note("老成员的笔记", author=405)
        it = await self.read(user=404, uid=40)
        self.assertEqual(it.sent[0].content, "📔 **用户404 记录了：**")
        self.assertEqual(self.contents(it), ["我走了但笔记还在"])
        it = await self.read(uid=41, members={1})
        authors = [e.author.name for m in it.sent for e in m.embeds]
        self.assertEqual(authors, ["旧成员405 记录了：", "用户404 记录了："])

    async def test_rendering_safe_and_split(self):
        await self.note("**粗体** [链接](https://evil) > 引用", author=1)
        it = await self.read(uid=50)
        self.assertEqual(self.contents(it), [r"\*\*粗体\*\* \[链接](https://evil) > 引用"])  # 行中 > 不是引用
        self.assertEqual(render_results([{"user_id": "1", "content": "> 行首引用", "created_at": "2026-10-22T01:00:00Z"}],
                                        {"1": "某人"}, None)[0]["embeds"][0].description, r"\> 行首引用")
        entries = [{"user_id": "1", "content": "喵" * 1000, "created_at": "2026-10-22T01:00:00Z"} for _ in range(5)]
        entries += [{"user_id": "1", "content": "**" * 500, "created_at": "2026-10-22T01:00:00Z"}]
        msgs = render_results(entries, {"1": "某人"}, None)
        self.assertGreater(len(msgs), 1)
        for m in msgs:
            self.assertLessEqual(sum(len(e) for e in m["embeds"]), 6000)
            self.assertLessEqual(len(m["embeds"]), 10)

    async def test_original_content_never_changed(self):
        await self.note("今天吃了薯餅ABC", author=1)
        await self.read(text="薯饼abc", uid=60)
        self.assertEqual(self.entries()[0]["content"], "今天吃了薯餅ABC")


# ---------------------------------------------------------------- 隔离与重启

class IsolationTests(DiaryTestBase):
    async def test_guild_isolation(self):
        await self.note("G1 的笔记", author=1, guild=G1)
        await self.note("G2 的笔记", author=1, guild=G2)
        r = await self.write(f"记笔记 <@{BOT}> 写到 G2 频道", guild=G1, channel=CH2, mid=77)
        self.assertEqual(r, [])                                                  # 别的服务器的频道不算
        self.assertEqual([e.description for m in (await self.read(guild=G1)).sent for e in m.embeds], ["G1 的笔记"])
        self.assertEqual([e.description for m in (await self.read(guild=G2)).sent for e in m.embeds], ["G2 的笔记"])
        self.assertEqual(len(self.entries(G1)), 1)
        self.assertEqual(len(self.entries(G2)), 1)

    async def test_cooldowns_independent_and_survive_restart(self):
        await self.write(f"记笔记 <@{BOT}> 写", mid=1)
        await self.read(uid=1)                                                  # 写冷却不影响读
        self.cog = self.new_cog()                                               # 重启
        self.assertIn("秒后再来记", (await self.write(f"记笔记 <@{BOT}> 再写", mid=2))[0][0])
        self.assertIn("秒后再来", (await self.read(uid=1)).sent[0].content)
        r = await self.write(f"记笔记 <@{BOT}> 别的服", guild=G2, channel=CH2, mid=3)
        self.assertIn("已经帮你记入", r[0][0])                                    # 不同服务器冷却隔离

    async def test_disabled_guild(self):
        self.cfg["guilds"][str(G1)]["enabled"] = False
        self.assertEqual(await self.write(f"记笔记 <@{BOT}> x"), [])
        self.assertIn("还没开放", (await self.read()).sent[0].content)


class BotRoutingTests(unittest.IsolatedAsyncioTestCase):
    """bot.py 的 on_message：日事本消息不再触发找人用法提示和聊天关键词。"""

    async def test_diary_message_skips_legacy_handlers(self):
        import bot as bot_module
        me = NS(id=BOT)
        cases = [
            (f"记笔记 <@{BOT}> 你好玉猫", False),       # 日事本：跳过旧处理
            (f"<@{BOT}> 找 沈墨", True),                # 找人：照常
        ]
        for content, expect_legacy in cases:
            with self.subTest(content),                     mock.patch.object(type(bot_module.bot), "user", new=me),                     mock.patch.object(bot_module.responses, "handle_category_chat",
                                      new=mock.AsyncMock(return_value=False)) as chat,                     mock.patch.object(bot_module.responses, "handle_mention", new=mock.AsyncMock()) as mention,                     mock.patch.object(bot_module.bot, "process_commands", new=mock.AsyncMock()):
                msg = NS(content=content, author=NS(id=1), guild=NS(id=bot_module.config.PERSONA_GUILD_ID),
                         mentions=[me], webhook_id=None)
                await bot_module.on_message(msg)
                self.assertEqual(chat.await_count == 1, expect_legacy)
                self.assertEqual(mention.await_count == 1, expect_legacy)


if __name__ == "__main__":
    unittest.main()

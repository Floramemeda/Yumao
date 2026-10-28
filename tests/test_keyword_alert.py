"""论坛关键词提醒测试：python -m unittest discover tests"""
import unittest
from types import SimpleNamespace as NS

from minigames.keyword_alert.cog import KeywordAlert, find_keyword, load_config
from minigames.persona_proxy.webhook_sender import KNOWN_WEBHOOK_IDS

CFG = load_config()
WATCH = CFG["watches"][0]
GUILD, FORUM, ROLE = WATCH["guild_id"], WATCH["forum_channel_ids"][0], WATCH["alert_role_id"]
BOT = 999


def message(content, thread=500, parent=FORUM, guild=GUILD, author=1, bot=False, webhook=None, mid=None):
    msg = NS(content=content, id=mid or id(content) + thread, guild=NS(id=guild),
             channel=NS(id=thread, parent_id=parent), author=NS(id=author, bot=bot), webhook_id=webhook, replies=[])

    async def reply(text, mention_author=True, allowed_mentions=None):
        msg.replies.append((text, mention_author, allowed_mentions))
    msg.reply = reply
    return msg


class MatchTests(unittest.TestCase):
    def test_keywords_and_variants(self):
        kws = WATCH["keywords"]
        for text, kw in (("可以私聊吗", "私聊"), ("私 聊一下", "私聊"), ("私聊", "私聊"),
                         ("求加好友～", "加好友"), ("加加我", "加加"), ("加 好 友", "加好友")):
            with self.subTest(text):
                self.assertEqual(find_keyword(text, kws), kw)
        for text in ("今天剧情很好看", "加入招募", "好友在吗", "私下说", ""):
            with self.subTest(text):
                self.assertIsNone(find_keyword(text, kws))


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = [0.0]
        self.cog = KeywordAlert(NS(user=NS(id=BOT)), CFG, clock=lambda: self.now[0])

    async def send(self, *a, **kw):
        m = message(*a, **kw)
        await self.cog.on_message(m)
        return m.replies

    async def test_alert_in_forum_thread(self):
        text, mention_author, am = (await self.send("有人私聊吗"))[0]
        self.assertEqual(text, f"<@&{ROLE}> ⚠️ 检测到关键词「私聊」")
        self.assertFalse(mention_author)
        self.assertEqual(([r.id for r in am.roles], am.everyone, am.users), ([ROLE], False, False))

    async def test_scope(self):
        self.assertEqual(await self.send("私聊", parent=123), [])                    # 其他论坛/频道的子区
        self.assertEqual(await self.send("私聊", parent=None, thread=FORUM), [])     # 不是帖子
        self.assertEqual(await self.send("私聊", guild=42), [])                      # 其他服务器
        self.assertEqual(await self.send("没问题"), [])

    async def test_senders(self):
        self.assertEqual(await self.send("私聊", author=BOT, bot=True), [])          # 玉猫自己
        self.assertEqual(await self.send("私聊", thread=501, bot=True), [])          # 其他机器人
        self.assertEqual(await self.send("私聊", thread=502, bot=True, webhook=77), [])  # 其他 Webhook
        KNOWN_WEBHOOK_IDS.add(8888)
        self.assertTrue(await self.send("私聊", thread=503, bot=True, webhook=8888))   # 皮套：背后是真人

    async def test_cooldown_per_thread_and_dedupe(self):
        self.assertTrue(await self.send("私聊", mid=1))
        self.now[0] += 30
        self.assertEqual(await self.send("加好友", mid=2), [])                       # 同帖 60 秒内
        self.assertTrue(await self.send("加加", thread=600, mid=3))                  # 其他帖子不受影响
        self.now[0] += 31
        self.assertTrue(await self.send("加好友", mid=4))
        m = message("私聊", thread=700, mid=5)
        await self.cog.on_message(m)
        self.now[0] += 100
        await self.cog.on_message(m)                                                 # 同一消息重复投递
        self.assertEqual(len(m.replies), 1)

    async def test_cooldown_can_be_disabled(self):
        self.cog.cfg = dict(CFG, cooldown_seconds_per_thread=0)
        self.assertTrue(await self.send("私聊", mid=10))
        self.assertTrue(await self.send("私聊", mid=11))


if __name__ == "__main__":
    unittest.main()

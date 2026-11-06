"""阵营战测试：python -m unittest discover tests"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import discord

from minigames.camp_war.cog import CampWar, load_config

CFG = load_config()
GUILD = 1541312608114581576
GCFG = CFG["guilds"][str(GUILD)]
TIGER, DRAGON = (int(c["role_id"]) for c in GCFG["camps"])
BOT, MSG, CH = 999, 5000, int(GCFG["channel_id"])


class FakeMember:
    def __init__(self, uid, roles=(), bot=False):
        self.id, self.bot = uid, bot
        self.roles = [NS(id=r) for r in roles]
        self.added = []
        self.fail = False

    async def add_roles(self, role, reason=None):
        if self.fail:
            raise discord.HTTPException(NS(status=403, reason="x"), "x")
        await asyncio.sleep(0)                     # 让并发的点击有机会插进来
        self.added.append(role.id)


class FakeChannel:
    def __init__(self):
        self.removed = []
        self.sent = []

    def get_partial_message(self, mid):
        async def remove_reaction(emoji, member):
            self.removed.append((mid, emoji.name, member.id))
        return NS(remove_reaction=remove_reaction)

    async def send(self, embed=None):
        self.sent.append(embed)
        reactions = []

        async def add_reaction(e):
            reactions.append(e.name)
        return NS(id=MSG + len(self.sent), jump_url="https://x/m", add_reaction=add_reaction, reactions=reactions)


EMOJI_IDS = {c["emoji"]: int(c["emoji_id"]) for c in GCFG["camps"]}


def payload(member, emoji, mid=MSG):
    return NS(message_id=mid, guild_id=GUILD, channel_id=CH, user_id=member.id, member=member,
              emoji=NS(name=emoji, id=EMOJI_IDS.get(emoji)))


class CampWarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "messages.json"
        self.channel = FakeChannel()
        roles = {TIGER: NS(id=TIGER), DRAGON: NS(id=DRAGON)}
        self.emojis = [NS(name=c["emoji"], id=int(c["emoji_id"])) for c in GCFG["camps"]]
        self.guild = NS(id=GUILD, get_role=roles.get, emojis=self.emojis,
                        get_channel=lambda cid: self.channel if cid == CH else None)
        self.bot_emojis = list(self.emojis)
        bot = NS(user=NS(id=BOT), get_emoji=lambda i: next((e for e in self.bot_emojis if e.id == i), None),
                 emojis=self.bot_emojis, get_guild=lambda gid: self.guild if gid == GUILD else None,
                 get_channel=lambda cid: self.channel if cid == CH else None)
        self.cog = CampWar(bot, CFG, self.file)
        self.cog.messages[str(MSG)] = str(GUILD)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_first_pick_gets_role_and_keeps_reaction(self):
        m = FakeMember(1)
        await self.cog.on_raw_reaction_add(payload(m, "TB_conghu"))
        self.assertEqual((m.added, self.channel.removed), ([TIGER], []))

    async def test_later_clicks_removed(self):
        m = FakeMember(1, roles=[DRAGON])
        await self.cog.on_raw_reaction_add(payload(m, "TB_conghu"))       # 已是龙骧军，点虎贲：撤掉
        await self.cog.on_raw_reaction_add(payload(m, "😀"))              # 无关表情：撤掉
        await self.cog.on_raw_reaction_add(payload(m, "TB_conglong"))     # 点回自己阵营：保留
        fake = payload(m, "TB_conglong")
        fake.emoji.id = 123                                                # 别处同名表情：不算
        await self.cog.on_raw_reaction_add(fake)
        self.assertEqual(m.added, [])
        self.assertEqual([r[1] for r in self.channel.removed], ["TB_conghu", "😀", "TB_conglong"])

    async def test_fast_double_click_only_one_camp(self):
        m = FakeMember(1)
        await asyncio.gather(self.cog.on_raw_reaction_add(payload(m, "TB_conghu")),
                             self.cog.on_raw_reaction_add(payload(m, "TB_conglong")))
        self.assertEqual(m.added, [TIGER])
        self.assertEqual([r[1] for r in self.channel.removed], ["TB_conglong"])

    async def test_unrelated_messages_bots_and_failures(self):
        m = FakeMember(1)
        await self.cog.on_raw_reaction_add(payload(m, "TB_conghu", mid=1))     # 不是阵营消息
        await self.cog.on_raw_reaction_add(payload(FakeMember(BOT), "TB_conghu"))
        await self.cog.on_raw_reaction_add(payload(FakeMember(2, bot=True), "TB_conghu"))
        self.assertEqual((m.added, self.channel.removed), ([], []))
        bad = FakeMember(3)
        bad.fail = True                                                         # 加身份组失败：撤掉表情、不记阵营
        await self.cog.on_raw_reaction_add(payload(bad, "TB_conghu"))
        self.assertEqual(self.channel.removed, [(MSG, "TB_conghu", 3)])
        bad.fail = False
        await self.cog.on_raw_reaction_add(payload(bad, "TB_conglong"))
        self.assertEqual(bad.added, [DRAGON])

    async def test_command(self):
        def interaction(admin=True, emojis=None):
            it = NS(guild_id=GUILD, guild=NS(emojis=self.emojis if emojis is None else emojis,
                                              get_channel=self.guild.get_channel),
                    permissions=NS(administrator=admin, manage_guild=False, manage_messages=False), sent=[])

            async def send_message(text, ephemeral=False):
                it.sent.append(text)

            async def defer(**kw):
                pass
            it.response = NS(send_message=send_message, defer=defer)
            it.followup = NS(send=lambda text, ephemeral=False: send_message(text))
            return it

        it = interaction(admin=False)
        await self.cog.camp_war.callback(self.cog, it)
        self.assertIn("只有管理员", it.sent[0])
        self.bot_emojis.clear()                                                    # 玉猫不在表情所在服务器
        it = interaction()
        await self.cog.camp_war.callback(self.cog, it)
        self.assertIn("TB_conghu", it.sent[0])
        self.bot_emojis.extend(self.emojis)
        it = interaction()
        await self.cog.camp_war.callback(self.cog, it, title="道友最近感觉：")
        self.assertIn("阵营战已发起", it.sent[-1])
        text = self.channel.sent[0].description
        self.assertTrue(text.startswith("道友最近感觉：\n\n"))
        self.assertIn("**虎贲军** — 炼丹谷", text)
        self.assertIn("**龙骧军** — 皇族", text)
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8"))[str(MSG + 1)], str(GUILD))
        reloaded = CampWar(self.cog.bot, CFG, self.file)                           # 重启后仍认得这条消息
        self.assertIn(str(MSG + 1), reloaded.messages)


if __name__ == "__main__":
    unittest.main()

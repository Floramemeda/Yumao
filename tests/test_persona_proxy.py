"""皮套系统测试：python -m unittest discover tests"""
import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import discord

from minigames.persona_proxy import cog as cog_mod
from minigames.persona_proxy.cog import PersonaProxy, ProfilePicker, TextModal, modal_values
from minigames.persona_proxy.service import (
    FAILED, PARTIAL, SENT, UNKNOWN, PersonaError, PersonaService, load_config,
)
from minigames.persona_proxy.store import PersonaStore, StorageError
from minigames.persona_proxy.webhook_sender import (
    KNOWN_WEBHOOK_IDS, SendFailed, SendUnknown, WebhookSender, check_channel, is_persona_webhook,
)

G, G2 = 100, 200
ROLE = 7
ADMIN = 8
DISPLAY = 300
LOGT = 400
CH = 500
BASE = datetime(2026, 10, 22, 7, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = BASE

    def __call__(self):
        self.now += timedelta(seconds=1)      # 每次取时间都前进，保证创建顺序稳定
        return self.now


def make_cfg():
    cfg = copy.deepcopy(load_config())
    cfg["guilds"] = {str(gid): {"enabled": True, "allowed_role_ids": [ROLE, ADMIN], "unlimited_role_ids": [ADMIN],
                                "profile_display_channel_id": DISPLAY, "creation_log_thread_id": LOGT}
                     for gid in (G, G2)}
    return cfg


def make_service(tmp, cfg=None):
    return PersonaService(cfg or make_cfg(), PersonaStore(Path(tmp)), clock=Clock())


AVATAR = "https://example.org/a.png"


# ---------------------------------------------------------------- 规则层

class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = make_service(self.tmp.name)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def create(self, uid=1, name="释无相", gid=G, roles=(ROLE,)):
        return await self.svc.create_profile(gid, uid, roles, name, AVATAR, "描述")

    async def test_default_limit_one(self):
        await self.create()
        with self.assertRaisesRegex(PersonaError, "上限 1"):
            await self.create(name="第二个")

    async def test_role_gate_fail_closed(self):
        await self.svc.grant(G, 1, 5)
        with self.assertRaisesRegex(PersonaError, "身份组"):
            await self.create(roles=())
        cfg = make_cfg()
        cfg["guilds"][str(G)]["allowed_role_ids"] = []        # 没配置准入身份组：谁都不行
        svc = make_service(self.tmp.name, cfg)
        with self.assertRaisesRegex(PersonaError, "身份组"):
            await svc.create_profile(G, 1, (ROLE,), "x", AVATAR, "")

    async def test_limit_zero_disables_but_keeps(self):
        p, _, _ = await self.create()
        await self.svc.grant(G, 1, 0)
        with self.assertRaisesRegex(PersonaError, "配额是 0"):
            self.svc.require_speaker(G, 1, (ROLE,), p["id"])
        self.assertEqual(len(self.svc.owned(self.svc.store.load(G), 1)), 1)
        await self.svc.edit_profile(G, 1, p["id"], "改名", AVATAR, "")       # 本人仍可维护

    async def test_downgrade_keeps_earliest_and_restores(self):
        await self.svc.grant(G, 1, 5)
        ids = [(await self.create(name=f"角色{i}"))[0]["id"] for i in range(5)]
        r = await self.svc.grant(G, 1, 3)
        self.assertEqual((r["old"], r["new"], r["count"], r["usable"]), (5, 3, 5, 3))
        doc = self.svc.store.load(G)
        self.assertEqual([p["id"] for p in self.svc.usable(doc, 1)], ids[:3])
        with self.assertRaisesRegex(PersonaError, "超出"):
            self.svc.require_speaker(G, 1, (ROLE,), ids[4])
        with self.assertRaisesRegex(PersonaError, "达到上限"):
            await self.create(name="绕过")                                       # 不能借创建绕过
        await self.svc.grant(G, 1, 5)
        self.assertEqual(len(self.svc.usable(self.svc.store.load(G), 1)), 5)

    async def test_guild_isolation_and_ownership(self):
        p, _, _ = await self.create(uid=1)
        with self.assertRaisesRegex(PersonaError, "找不到"):
            self.svc.require_speaker(G, 2, (ROLE,), p["id"])                    # 冒用他人 profile id
        with self.assertRaisesRegex(PersonaError, "找不到"):
            self.svc.require_speaker(G2, 1, (ROLE,), p["id"])                   # 跨服务器
        with self.assertRaises(PersonaError):
            await self.svc.edit_profile(G, 2, p["id"], "x", AVATAR, "")
        with self.assertRaises(PersonaError):
            await self.svc.delete_profile(G, 2, p["id"])
        await self.create(uid=1, gid=G2)                                        # G2 配额独立

    async def test_unlimited_role(self):
        admin = (ADMIN,)
        for i in range(4):
            await self.svc.create_profile(G, 9, admin, f"管{i}", AVATAR, "")
        r = await self.svc.grant(G, 9, 0, admin)                                  # grant 对其不生效
        self.assertTrue(r["unlimited"])
        self.assertEqual(r["usable"], 4)
        doc = self.svc.store.load(G)
        self.assertEqual(self.svc.limit_of(doc, 9, admin), 50)
        ids = [p["id"] for p in self.svc.owned(doc, 9)]
        self.svc.require_speaker(G, 9, admin, ids[3])
        # 身份组被撤（只剩普通准入身份组）：回到 grant 的 0
        with self.assertRaisesRegex(PersonaError, "配额是 0"):
            self.svc.require_speaker(G, 9, (ROLE,), ids[0])
        await self.svc.grant(G, 9, 2, (ROLE,))
        self.assertEqual([p["id"] for p in self.svc.usable(self.svc.store.load(G), 9, (ROLE,))], ids[:2])
        with self.assertRaisesRegex(PersonaError, "超出"):
            self.svc.require_speaker(G, 9, (ROLE,), ids[3])

    async def test_concurrent_create_respects_quota(self):
        results = await asyncio.gather(*(self.create(name=f"并发{i}") for i in range(5)), return_exceptions=True)
        self.assertEqual(sum(not isinstance(r, Exception) for r in results), 1)

    async def test_delete_releases_slot(self):
        p, _, _ = await self.create()
        await self.svc.delete_profile(G, 1, p["id"])
        await self.create(name="新的")

    def test_validation(self):
        v = self.svc
        for bad in ("", "a" * 81, "Discord 官方", "管理员小猫", "@everyone", "名字#1", "坏\n名", "everyone"):
            with self.subTest(bad):
                with self.assertRaises(PersonaError):
                    v.validate_name(bad)
        self.assertEqual(v.validate_name(" 释无相 "), "释无相")
        for bad in ("", "ftp://x/a.png", "http://localhost/a.png", "http://127.0.0.1/a", "http://10.0.0.5/a",
                    "http://169.254.169.254/latest", "https://user:pw@example.org/a", "javascript:alert(1)",
                    "https://example.org/" + "a" * 1100):
            with self.subTest(bad):
                with self.assertRaises(PersonaError):
                    v.validate_avatar(bad)
        self.assertEqual(v.validate_avatar(AVATAR), AVATAR)
        with self.assertRaises(PersonaError):
            v.validate_description("x" * 2001)
        with self.assertRaises(PersonaError):
            v.validate_message("x" * 2001)

    async def test_draft_idempotent_and_claim_once(self):
        d1 = await self.svc.save_draft(G, 1, "p_x", CH, "say", "长文", draft_id="d_same")
        d2 = await self.svc.save_draft(G, 1, "p_x", CH, "say", "另一个", draft_id="d_same")
        self.assertEqual(d2["text"], "长文")
        claims = await asyncio.gather(*(self.svc.claim(G, 1, d1["id"]) for _ in range(5)), return_exceptions=True)
        self.assertEqual(sum(not isinstance(c, Exception) for c in claims), 1)
        with self.assertRaises(PersonaError):
            await self.svc.claim(G, 2, d1["id"])                                 # 别人的草稿

    async def test_corrupt_file_not_overwritten(self):
        p = Path(self.tmp.name) / f"{G}.json"
        p.write_text("{bad", encoding="utf-8")
        with self.assertRaises(StorageError):
            await self.create()
        self.assertEqual(p.read_text(encoding="utf-8"), "{bad")


# ---------------------------------------------------------------- Discord 伪造

class FakeTextChannel(discord.TextChannel):
    def __init__(self, cid, messages=()):
        """messages：可回复的消息 id 集合，或 {id: 消息对象}；集合里的消息默认是真人 55 发的。"""
        self.id = cid
        self.messages = dict(messages) if isinstance(messages, dict) else {m: human_msg(m) for m in messages}

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise discord.NotFound(NS(status=404, reason="nf"), "nf")
        return self.messages[mid]


def human_msg(mid, uid=55):
    return NS(id=mid, webhook_id=None, author=NS(id=uid, bot=False, display_name=f"玩家{uid}"))


class FakeThread(discord.Thread):
    def __init__(self, tid, guild_id=G, locked=False, fail=False):
        self.id = tid
        self.guild = NS(id=guild_id)
        self.locked = locked
        self.fail = fail
        self.sent = []

    async def send(self, embed=None, allowed_mentions=None, **kw):
        if self.fail:
            raise discord.HTTPException(NS(status=403, reason="no"), "no")
        self.sent.append(embed)
        return NS(id=999)


class FakeSender:
    def __init__(self):
        self.sent = []
        self.mentions = []
        self.fail_on: dict[int, Exception] = {}
        self.locks = {}

    def channel_lock(self, cid):
        return self.locks.setdefault(cid, asyncio.Lock())

    async def send(self, channel, profile, content, mention_user_id=None):
        exc = self.fail_on.pop(len(self.sent), None)
        if exc:
            raise exc
        self.sent.append((channel.id, profile["name"], profile["avatar_url"], content))
        self.mentions.append(mention_user_id)
        return 10_000 + len(self.sent)


def perms(**kw):
    base = dict(view_channel=True, send_messages=True, send_messages_in_threads=True, manage_threads=False,
                manage_guild=False, administrator=False, manage_webhooks=True)
    base.update(kw)
    return NS(**base)


class FakeInteraction:
    def __init__(self, uid=1, roles=(ROLE,), guild=G, channel=None, data=None, itype=None, **perm):
        self.user = NS(id=uid, roles=[NS(id=r) for r in roles])
        self.guild_id = guild
        self.channel = channel or FakeTextChannel(CH, messages={42})
        self.channel_id = self.channel.id
        self.permissions = perms(**perm)
        self.app_permissions = perms()
        self.data = data or {}
        self.type = itype or discord.InteractionType.application_command
        self.messages, self.modal, self.deferred, self._done = [], None, False, False
        self.response = NS(is_done=lambda: self._done, defer=self._defer, send_message=self._send,
                           send_modal=self._modal, edit_message=self._edit)
        self.followup = NS(send=self._follow)

    async def _defer(self, **kw):
        self.deferred, self._done = True, True

    async def _send(self, content=None, **kw):
        self._done = True
        self.messages.append(NS(content=content, **kw))

    async def _modal(self, modal):
        assert not self._done, "不能在已响应的 interaction 上打开 Modal"
        self._done, self.modal = True, modal

    async def _edit(self, **kw):
        self.messages.append(NS(content=kw.get("content"), **kw))

    async def _follow(self, content=None, **kw):
        self.messages.append(NS(content=content, **kw))

    @property
    def last(self):
        return self.messages[-1].content


def submit(modal_custom_id, text, **kw):
    data = {"custom_id": modal_custom_id,
            "components": [{"type": 1, "components": [{"type": 4, "custom_id": "text", "value": text}]}]}
    return FakeInteraction(data=data, itype=discord.InteractionType.modal_submit, **kw)


class CogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = make_service(self.tmp.name)
        self.sender = FakeSender()
        self.log_thread = FakeThread(LOGT)
        self.bot = NS(get_channel=lambda cid: self.log_thread if cid == LOGT else None, user=NS(id=1))
        self.cog = PersonaProxy(self.bot, self.svc, self.sender)
        self.patch = mock.patch.object(cog_mod, "check_channel", lambda i: None)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    async def profile(self, uid=1, name="释无相", desc="姓名：释无相\n身份：佛门高弟"):
        p, _, _ = await self.svc.create_profile(G, uid, (ROLE,), name, AVATAR, desc)
        return p

    async def say(self, pid, message=None, **kw):
        it = FakeInteraction(**kw)
        await self.cog.say.callback(self.cog, it, profile=pid, message=message)
        return it

    def drafts(self):
        return list(self.svc.store.load(G)["drafts"].values())

    # ---- /say

    async def test_say_direct(self):
        p = await self.profile()
        it = await self.say(p["id"], "大家好")
        self.assertEqual(self.sender.sent, [(CH, "释无相", AVATAR, "大家好")])
        self.assertIn("已用「释无相」发出", it.last)
        self.assertEqual(self.drafts()[0]["status"], SENT)
        rec = self.svc.store.find_audit(G, self.drafts()[0]["message_ids"][0])
        self.assertEqual((rec["owner_id"], rec["profile_id"]), ("1", p["id"]))

    async def test_say_modal_flow_with_new_interaction(self):
        p = await self.profile()
        it = await self.say(p["id"])
        self.assertIsInstance(it.modal, TextModal)
        self.assertFalse(it.deferred)                                            # 没有先 defer
        self.assertIsNone(it.modal.timeout)
        sub = submit(it.modal.custom_id, "很长很长的正文" * 100)
        await self.cog.on_interaction(sub)
        self.assertTrue(sub.deferred)
        self.assertEqual(self.sender.sent[0][3], "很长很长的正文" * 100)

    async def test_modal_submit_after_restart(self):
        p = await self.profile()
        it = await self.say(p["id"])
        cog2 = PersonaProxy(self.bot, make_service(self.tmp.name), self.sender)  # 重启：内存里没有 Modal
        sub = submit(it.modal.custom_id, "重启后提交")
        await cog2.on_interaction(sub)
        self.assertEqual(self.sender.sent[0][3], "重启后提交")

    async def test_duplicate_submit_sends_once(self):
        p = await self.profile()
        it = await self.say(p["id"])
        subs = [submit(it.modal.custom_id, "同一个输入框") for _ in range(3)]
        await asyncio.gather(*(self.cog.on_interaction(s) for s in subs))
        self.assertEqual(len(self.sender.sent), 1)

    async def test_role_revoked_after_modal_opened(self):
        p = await self.profile()
        it = await self.say(p["id"])
        sub = submit(it.modal.custom_id, "撤权后提交", roles=())
        await self.cog.on_interaction(sub)
        self.assertEqual(self.sender.sent, [])
        d = self.drafts()[0]
        self.assertEqual((d["status"], d["text"]), (FAILED, "撤权后提交"))           # 正文保住了
        self.assertIn("草稿", sub.last)

    async def test_failure_then_retry_once(self):
        p = await self.profile()
        self.sender.fail_on[0] = SendFailed("Discord 拒绝了这条消息（500）。")
        it = await self.say(p["id"], "第一次失败")
        self.assertIn("发送失败", it.last)
        d = self.drafts()[0]
        self.assertEqual(d["status"], FAILED)
        retries = [FakeInteraction() for _ in range(3)]
        await asyncio.gather(*(self.cog.say_retry.callback(self.cog, r, draft=d["id"]) for r in retries))
        self.assertEqual(len(self.sender.sent), 1)                                # 并发重试只发一次
        self.assertEqual(self.drafts()[0]["status"], SENT)

    async def test_unknown_result_not_auto_retried(self):
        p = await self.profile()
        self.sender.fail_on[0] = SendUnknown("网络异常，无法确认是否已发出。")
        it = await self.say(p["id"], "不确定")
        self.assertIn("不要重复发送", it.last)
        d = self.drafts()[0]
        self.assertEqual(d["status"], UNKNOWN)
        r = FakeInteraction()
        await self.cog.say_retry.callback(self.cog, r, draft=d["id"])
        self.assertIn("结果不明", r.last)
        self.assertEqual(self.sender.sent, [])
        v = FakeInteraction()
        await self.cog.say_draft.callback(self.cog, v, draft=d["id"])             # 原文仍可查看
        self.assertIn("不确定", v.messages[0].embed.description)

    async def test_other_users_profile_id_rejected(self):
        p = await self.profile(uid=1)
        it = await self.say(p["id"], "冒用", uid=2)
        self.assertIn("找不到", it.last)
        self.assertEqual(self.sender.sent, [])

    # ---- /send

    async def send(self, pid, channel_id=DISPLAY, uid=1):
        it = FakeInteraction(uid=uid, channel=FakeTextChannel(channel_id))
        await self.cog.send_showcase.callback(self.cog, it, profile=pid)
        return it

    async def test_send_two_messages_exact(self):
        p = await self.profile()
        await self.send(p["id"])
        self.assertEqual([s[3] for s in self.sender.sent], [p["description"], AVATAR])

    async def test_send_channel_and_empty_description(self):
        p = await self.profile()
        self.assertIn("只能在", (await self.send(p["id"], channel_id=CH)).last)
        q = await self.profile(uid=2, desc="")
        self.assertIn("没有人设描述", (await self.send(q["id"], uid=2)).last)
        cfg = make_cfg()
        cfg["guilds"][str(G)]["profile_display_channel_id"] = None
        self.svc.cfg = cfg
        self.assertIn("还没有配置", (await self.send(p["id"])).last)
        self.assertEqual(self.sender.sent, [])

    async def test_send_partial_and_resume_second_only(self):
        p = await self.profile()
        self.sender.fail_on[0] = SendFailed("x")
        await self.send(p["id"])
        self.assertEqual(self.sender.sent, [])                                    # 第一条失败不发第二条
        self.sender.fail_on[1] = SendFailed("y")
        d = self.drafts()[0]
        r = FakeInteraction(channel=FakeTextChannel(DISPLAY))
        await self.cog.say_retry.callback(self.cog, r, draft=d["id"])
        self.assertEqual(self.drafts()[0]["status"], PARTIAL)
        r = FakeInteraction(channel=FakeTextChannel(DISPLAY))
        await self.cog.say_retry.callback(self.cog, r, draft=d["id"])
        self.assertEqual([s[3] for s in self.sender.sent], [p["description"], AVATAR])   # 第一条没有重复
        self.assertEqual(self.drafts()[0]["status"], SENT)

    # ---- 右键回复

    async def test_reply_flow(self):
        p = await self.profile()
        await self.profile(uid=2, name="别人的")
        it = FakeInteraction()
        target = NS(id=42)
        await self.cog.reply_context(it, target)
        view = it.messages[0].view
        options = [o.value for o in view.children[0].options]
        self.assertEqual(options, [p["id"]])                                      # 只列本人可用皮套
        sel = FakeInteraction(data={"values": [p["id"]]})
        await view.on_select(sel)
        self.assertTrue(sel.modal.custom_id.startswith(f"pp:reply:{p['id']}:42:"))  # 用 Select 的新 interaction 开 Modal
        sub = submit(sel.modal.custom_id, "回你一句")
        await self.cog.on_interaction(sub)
        content = self.sender.sent[0][3]
        self.assertIn(f"回复 <@55>：[原消息](https://discord.com/channels/{G}/{CH}/42)\n", content)
        self.assertEqual(content.split("\n", 1)[1], "回你一句")
        self.assertEqual(self.sender.mentions, [55])                             # 只提醒被回复的真人

    async def test_reply_to_persona_or_bot_does_not_ping(self):
        p = await self.profile()
        targets = {
            61: NS(id=61, webhook_id=9000, author=NS(id=9000, bot=True, display_name="释*无相*")),   # 皮套消息
            62: NS(id=62, webhook_id=None, author=NS(id=1, bot=True, display_name="造化玉猫")),      # 机器人
        }
        for mid, name in ((61, r"**释\*无相\***"), (62, "**造化玉猫**")):
            with self.subTest(mid):
                sub = submit(f"pp:reply:{p['id']}:{mid}:n{mid}", "嗯", channel=FakeTextChannel(CH, messages=targets))
                await self.cog.on_interaction(sub)
                self.assertIn(f"回复 {name}：[原消息]", self.sender.sent[-1][3])
                self.assertIsNone(self.sender.mentions[-1])

    async def test_reply_target_gone_keeps_draft(self):
        p = await self.profile()
        sub = submit(f"pp:reply:{p['id']}:77:abc", "原消息没了", channel=FakeTextChannel(CH, messages=()))
        await self.cog.on_interaction(sub)
        self.assertEqual(self.sender.sent, [])
        self.assertIn("不可访问", sub.last)
        self.assertEqual(self.drafts()[0]["status"], FAILED)

    async def test_picker_paginates(self):
        profiles = [{"id": f"p_{i:010d}", "name": f"角色{i}"} for i in range(30)]
        v1 = ProfilePicker(self.cog, 1, profiles, NS(id=1))
        self.assertEqual(len(v1.children[0].options), 25)
        v2 = ProfilePicker(self.cog, 1, profiles, NS(id=1), page=1)
        self.assertEqual(len(v2.children[0].options), 5)

    # ---- 创建日志

    async def test_creation_log(self):
        it = FakeInteraction()
        await self.cog.submit_profile(it, "create", None, "释无相", AVATAR, "描述")
        embed = self.log_thread.sent[0]
        fields = {f.name: f.value for f in embed.fields}
        self.assertIn("<@1>", fields["创建者"])
        self.assertEqual(fields["配额"], "1 / 1")
        self.assertEqual(embed.thumbnail.url, AVATAR)
        p = self.svc.owned(self.svc.store.load(G), 1)[0]
        self.assertEqual((p["creation_log_status"], p["creation_log_message_id"]), ("sent", "999"))
        it2 = FakeInteraction()
        await self.cog.submit_profile(it2, "create", None, "第二个", AVATAR, "")   # 配额拒绝：不写日志
        self.assertEqual(len(self.log_thread.sent), 1)

    async def test_log_failure_does_not_roll_back(self):
        self.log_thread.fail = True
        await self.cog.submit_profile(FakeInteraction(), "create", None, "释无相", AVATAR, "")
        p = self.svc.owned(self.svc.store.load(G), 1)
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["creation_log_status"], "failed")

    # ---- 管理

    async def test_grant_requires_manage_guild(self):
        it = FakeInteraction()
        await self.cog.grant_profile.callback(self.cog, it, user=NS(id=2), limit=3)
        self.assertIn("管理服务器", it.last)
        it = FakeInteraction(manage_guild=True)
        await self.cog.grant_profile.callback(self.cog, it, user=NS(id=2), limit=3)
        self.assertIn("1 → **3**", it.last)

    async def test_audit_lookup_admin_only(self):
        p = await self.profile()
        await self.say(p["id"], "审计")
        mid = int(self.drafts()[0]["message_ids"][0])
        it = FakeInteraction()
        await self.cog.audit_context(it, NS(id=mid))
        self.assertIn("只有管理员", it.last)
        it = FakeInteraction(administrator=True)
        await self.cog.audit_context(it, NS(id=mid))
        self.assertIn("<@1>", it.last)
        self.assertIn(p["id"], it.last)

    async def test_list_shows_disabled(self):
        await self.svc.grant(G, 1, 2)
        await self.profile(name="A")
        await self.profile(name="B")
        await self.svc.grant(G, 1, 1)
        it = FakeInteraction(roles=())
        await self.cog.profile_list.callback(self.cog, it)
        desc = it.messages[0].embed.description
        self.assertIn("✅ 可用", desc)
        self.assertIn("暂时停用", desc)


# ---------------------------------------------------------------- 频道权限与 Webhook

class ChannelAndWebhookTests(unittest.IsolatedAsyncioTestCase):
    def test_check_channel(self):
        ok = FakeInteraction(channel=FakeTextChannel(CH))
        check_channel(ok)
        cases = [
            (FakeInteraction(channel=FakeTextChannel(CH), send_messages=False), "没有发言权限"),
            (FakeInteraction(channel=FakeTextChannel(CH), view_channel=False), "看不到"),
            (FakeInteraction(channel=FakeThread(1), send_messages_in_threads=False), "子区没有发言权限"),
            (FakeInteraction(channel=FakeThread(1, locked=True)), "锁定"),
        ]
        for it, msg in cases:
            with self.subTest(msg), self.assertRaisesRegex(PersonaError, msg):
                check_channel(it)
        it = FakeInteraction(channel=FakeTextChannel(CH))
        it.app_permissions = perms(manage_webhooks=False)
        with self.assertRaisesRegex(PersonaError, "管理 Webhook"):
            check_channel(it)
        check_channel(FakeInteraction(channel=FakeThread(1, locked=True), manage_threads=True))

    async def test_sender_reuses_and_recovers(self):
        sent, created = [], []

        class WH:
            def __init__(self, wid):
                self.id, self.name, self.token, self.user = wid, "玉猫皮套", "secret", NS(id=1)
                self.dead = False

            async def send(self, content, **kw):
                if self.dead:
                    raise discord.NotFound(NS(status=404, reason="nf"), "nf")
                sent.append((self.id, content, kw))
                return NS(id=len(sent))

        hooks = []

        class Parent(discord.TextChannel):
            def __init__(self):
                self.id, self.guild = CH, NS(id=G)

            async def webhooks(self):
                return list(hooks)

            async def create_webhook(self, name, reason=None):
                wh = WH(9000 + len(created))
                created.append(wh)
                hooks.append(wh)
                return wh

        remembered = []

        async def remember(g, c, w):
            remembered.append((g, c, w))

        s = WebhookSender(NS(user=NS(id=1)), "玉猫皮套", on_new_webhook=remember)
        parent = Parent()
        prof = {"name": "释无相", "avatar_url": AVATAR}
        await s.send(parent, prof, "一")
        await s.send(parent, prof, "二")
        self.assertEqual(len(created), 1)                                         # 复用同一个 webhook
        kw = sent[0][2]
        self.assertEqual((kw["username"], kw["avatar_url"], kw["wait"]), ("释无相", AVATAR, True))
        self.assertFalse(kw["allowed_mentions"].everyone)
        self.assertNotIn("thread", kw)
        self.assertTrue(is_persona_webhook(9000))
        self.assertEqual(remembered, [(G, CH, 9000)])
        hooks[0].dead = True
        hooks.clear()                                                             # 被删：重新创建一次
        await s.send(parent, prof, "三")
        self.assertEqual(len(created), 2)
        self.assertEqual(sent[-1][1], "三")
        await s.send(parent, prof, "四 @everyone <@&1>", mention_user_id=55)        # 回复：只允许提醒被回复者
        am = sent[-1][2]["allowed_mentions"]
        self.assertEqual(([u.id for u in am.users], am.everyone, am.roles), ([55], False, False))

    async def test_bot_ignores_persona_webhook_messages(self):
        import bot as bot_module
        KNOWN_WEBHOOK_IDS.add(5555)
        me = NS(id=1)
        with mock.patch.object(type(bot_module.bot), "user", new=me), \
                mock.patch.object(bot_module.responses, "handle_category_chat",
                                  new=mock.AsyncMock(return_value=False)) as chat, \
                mock.patch.object(bot_module.bot, "process_commands", new=mock.AsyncMock()):
            await bot_module.on_message(NS(content="你好玉猫", author=NS(id=2), guild=NS(id=1), webhook_id=5555,
                                           mentions=[]))
            self.assertEqual(chat.await_count, 0)
            await bot_module.on_message(NS(content="你好玉猫", author=NS(id=2), guild=NS(id=1), webhook_id=None,
                                           mentions=[]))
            self.assertEqual(chat.await_count, 1)

    def test_modal_values_both_layouts(self):
        rows = {"components": [{"type": 1, "components": [{"type": 4, "custom_id": "text", "value": "a"}]}]}
        label = {"components": [{"type": 18, "component": {"type": 4, "custom_id": "text", "value": "b"}}]}
        self.assertEqual(modal_values(rows), {"text": "a"})
        self.assertEqual(modal_values(label), {"text": "b"})


if __name__ == "__main__":
    unittest.main()

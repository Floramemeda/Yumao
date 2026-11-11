"""沙滩排球测试：python -m unittest discover tests"""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import discord

from minigames.beach_volleyball import engine, renderer, views
from minigames.beach_volleyball.models import END_IDLE, END_TARGET, END_WIPEOUT, PHASE_RECEIVE, PHASE_SERVE, Status, VolleyballSession
from minigames.beach_volleyball.service import VolleyballService

CFG = engine.load_config()
NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
CH = 4242


def cfg_with(chance):
    cfg = copy.deepcopy(CFG)
    for spec in cfg["parts"].values():
        spec["chance"] = chance
    return cfg


ALWAYS, NEVER = cfg_with(1.1), cfg_with(0)


def ff(cfg, chance):
    cfg = copy.deepcopy(cfg)
    cfg["friendly_fire"]["chance"] = chance
    return cfg


def game(team_a=(1, 2), team_b=(3, 4), target=30):
    """1、2 一队（发球方），3、4 一队。"""
    s = VolleyballSession(id="abc", guild_id=1, channel_id=CH, message_id=1, creator_user_id=1,
                          status=Status.IN_PROGRESS, created_at=NOW, target_score=target,
                          participants=[*team_a, *team_b], rng_seed="seed")
    s.teams = [list(team_a), list(team_b)]
    s.players = {str(u): engine.new_player() for u in s.participants}
    s.server_id, s.serving_team = team_a[0], 0
    return s


def play(s, cfg, direction, **choices):
    """choices: u3=("chest", "left")。"""
    engine.serve(s, direction)
    s.choices = {k[1:]: list(v) for k, v in choices.items()}
    return engine.resolve(s, cfg)


class EngineTests(unittest.TestCase):
    def test_start_splits_randomly_and_evenly(self):
        for n in range(2, 11):
            s = VolleyballSession(id="a", guild_id=1, channel_id=1, message_id=1, creator_user_id=1,
                                  status=Status.LOBBY, created_at=NOW, target_score=30, participants=list(range(n)))
            engine.start(s, f"seed{n}")
            with self.subTest(n):
                self.assertEqual(sorted(s.teams[0] + s.teams[1]), list(range(n)))
                self.assertLessEqual(abs(len(s.teams[0]) - len(s.teams[1])), 1)
                self.assertTrue(s.teams[0] and s.teams[1])
                self.assertIn(s.server_id, s.teams[s.serving_team])

    def test_part_scores_sum_across_team(self):
        s = game()
        e = play(s, ALWAYS, "left", u3=("chest", "left"), u4=("hand", "left"))
        self.assertEqual((e["gained"], s.scores), (4, [0, 4]))
        self.assertEqual(s.players["3"]["points"], 3)

    def test_nobody_caught_gives_server_one(self):
        s = game()
        e = play(s, ALWAYS, "left", u3=("hand", "right"), u4=("chest", "right"))
        self.assertEqual((s.scores, e["miss_score"]), ([1, 0], 1))
        s = game()
        play(s, NEVER, "left", u3=("hand", "left"))                # 猜中但没接住
        self.assertEqual(s.scores, [1, 0])

    def test_wrong_direction_no_damage_dropped_still_damages(self):
        s = game()
        play(s, ALWAYS, "left", u3=("chest", "right"))
        self.assertTrue(s.players["3"]["top"])
        s = game()
        e = play(s, NEVER, "left", u3=("butt", "left"))
        self.assertFalse(s.players["3"]["bottom"])
        self.assertTrue(s.players["3"]["top"])
        self.assertEqual((e["results"][0]["result"], e["results"][0]["broke"]), ("dropped", "bottom"))

    def test_hand_never_damages(self):
        s = game()
        for _ in range(5):
            s.server_id, s.serving_team = 1, 0
            play(s, ALWAYS, "left", u3=("hand", "left"))
        p = s.players["3"]
        self.assertEqual((p["top"], p["bottom"], p["excitement"]), (True, True, 0))

    def test_excitement_steps_then_out(self):
        s = game()
        seen = []
        for _ in range(4):                                          # 第 1 次坏衣服，之后 25 / 50 / 100
            s.server_id, s.serving_team = 1, 0
            play(s, NEVER, "left", u3=("chest", "left"))
            seen.append(s.players["3"]["excitement"])
        self.assertEqual(seen, [0, 25, 50, 100])
        self.assertTrue(s.players["3"]["out"])
        self.assertEqual(engine.receivers(s) if s.serving_team == 0 else None, [4])

    def test_excitement_shared_between_top_and_bottom(self):
        s = game()
        s.players["3"].update(top=False, bottom=False)
        play(s, NEVER, "left", u3=("chest", "left"))
        s.server_id, s.serving_team = 1, 0
        play(s, NEVER, "left", u3=("butt", "left"))
        self.assertEqual(s.players["3"]["excitement"], 50)

    def test_friendly_fire_hits_teammate(self):
        s = game(team_b=(3, 4, 5))
        e = play(s, ff(NEVER, 1.1), "left", u3=("hand", "right"))       # 猜错：必定误伤
        hit = e["results"][0]["friendly"]
        self.assertIn(hit["target"], (4, 5))
        self.assertIn(hit["part"], ("chest", "butt"))
        garment = "top" if hit["part"] == "chest" else "bottom"
        self.assertEqual(hit["broke"], garment)
        self.assertFalse(s.players[str(hit["target"])][garment])
        self.assertTrue(s.players["3"]["top"] and s.players["3"]["bottom"])   # 不会砸到自己

    def test_friendly_fire_only_from_wrong_or_dropped(self):
        s = game()
        e = play(s, ff(ALWAYS, 1.1), "left", u3=("hand", "left"))       # 接住 + 4 号发呆
        self.assertTrue(all("friendly" not in r for r in e["results"]))
        s = game()
        e = play(s, ff(NEVER, 1.1), "left", u3=("hand", "left"))        # 猜中没接住：会误伤
        self.assertEqual(e["results"][0]["friendly"]["target"], 4)
        s = game(team_b=(3,))
        e = play(s, ff(NEVER, 1.1), "left", u3=("hand", "right"))       # 没有队友
        self.assertNotIn("friendly", e["results"][0])
        s = game()
        e = play(s, ff(NEVER, 0), "left", u3=("hand", "right"))
        self.assertNotIn("friendly", e["results"][0])

    def test_friendly_fire_excitement_and_wipeout(self):
        s = game()
        for u in ("3", "4"):
            s.players[u].update(top=False, bottom=False, excitement=50)
        e = play(s, ff(NEVER, 1.1), "left", u3=("chest", "left"))       # 3 自己失能，又把 4 砸到失能
        self.assertEqual(e["results"][0]["friendly"]["excitement"], [50, 100])
        self.assertTrue(s.players["4"]["out"])
        self.assertEqual((s.winner, s.end_reason), (0, END_WIPEOUT))
        text = renderer.report_embed(e).description
        self.assertIn("球砸到了队友 <@4> 的", text)
        self.assertIn("发情度 50 → 100 · 💤 失能退场！", text)

    def test_real_friendly_fire_config(self):
        self.assertEqual(CFG["friendly_fire"], {"chance": 0.25, "parts": ["chest", "butt"]})

    def test_target_win(self):
        s = game(target=10)
        s.scores = [0, 9]
        e = play(s, ALWAYS, "left", u3=("hand", "left"))
        self.assertTrue(e["finished"])
        self.assertEqual((s.winner, s.end_reason), (1, END_TARGET))

    def test_wipeout(self):
        s = game(team_a=(1,), team_b=(3,))
        s.players["3"].update(top=False, excitement=50)
        e = play(s, NEVER, "left", u3=("chest", "left"))
        self.assertTrue(e["finished"])
        self.assertEqual((s.winner, s.end_reason), (0, END_WIPEOUT))

    def test_catch_hands_serve_to_a_catcher(self):
        s = game()
        e = play(s, ALWAYS, "left", u3=("hand", "left"), u4=("hand", "right"))
        self.assertEqual((s.serving_team, s.server_id, e["next_server"]), (1, 3, 3))
        self.assertEqual((s.phase, s.direction, s.choices), (PHASE_SERVE, None, {}))

    def test_miss_rotates_serving_team_skipping_out(self):
        s = game(team_a=(1, 2, 5))
        s.players["2"]["out"] = True
        play(s, ALWAYS, "left", u3=("hand", "right"))
        self.assertEqual((s.serving_team, s.server_id), (0, 5))
        play(s, ALWAYS, "left", u3=("hand", "right"))
        self.assertEqual(s.server_id, 1)

    def test_out_catcher_cannot_serve(self):
        s = game()
        s.players["3"].update(top=False, excitement=50)
        play(s, ALWAYS, "left", u3=("chest", "left"))
        self.assertEqual((s.serving_team, s.server_id), (1, 4))

    def test_idle_rounds_end_game(self):
        s = game()
        for i in range(CFG["max_idle_rounds"]):
            engine.serve(s, "left", auto=True)
            e = engine.resolve(s, CFG)
        self.assertTrue(e["finished"])
        self.assertEqual((s.end_reason, s.winner), (END_IDLE, None))
        renderer.final_embed(s)

    def test_active_play_resets_idle(self):
        s = game()
        s.idle_rounds = 2
        engine.serve(s, "left", auto=True)
        s.choices = {"3": ["hand", "right"]}
        engine.resolve(s, CFG)
        self.assertEqual(s.idle_rounds, 0)

    def test_replay_is_deterministic(self):
        def run():
            s = game()
            return play(s, CFG, "left", u3=("chest", "left"), u4=("hand", "left"))
        self.assertEqual(run(), run())

    def test_real_chances_ordered(self):
        p = CFG["parts"]
        self.assertGreater(p["hand"]["chance"], max(p["chest"]["chance"], p["butt"]["chance"]))
        self.assertEqual(set(p), {"hand", "chest", "butt"})
        self.assertEqual((p["hand"]["score"], p["chest"]["score"], p["butt"]["score"]), (1, 3, 3))

    def test_session_roundtrip(self):
        s = game()
        engine.serve(s, "right")
        s.choices = {"3": ["hand", "left"]}
        back = VolleyballSession.from_dict(s.to_dict())
        self.assertEqual(back.to_dict(), s.to_dict())


class RenderTests(unittest.TestCase):
    def test_panel_never_shows_secret(self):
        s = game()
        engine.serve(s, "right")
        s.choices = {"3": ["chest", "left"]}
        text = str(renderer.panel_embed(s, CFG).to_dict())
        self.assertNotIn("右", text.replace("发向", ""))
        self.assertNotIn("胸", text)
        self.assertIn("1/2", text)

    def test_report_lines(self):
        s = game()
        e = play(s, ALWAYS, "left", u3=("chest", "left"), u4=("hand", "right"))
        text = renderer.report_embed(e).description
        self.assertIn("<@3> 用胸接左边，接住了！+3 · 上衣坏了！", text)
        self.assertIn("<@4> 用手接右边，方向猜错了", text)
        self.assertIn("发向 **左边**", text)

    def test_views_fit_discord_limits(self):
        s = game()
        view = views.game_view(s)
        self.assertEqual(len(view.children), 3)
        engine.serve(s, "left")
        view = views.game_view(s)
        self.assertEqual([c.item.label for c in view.children], ["接左边", "接右边", "取消本局"])
        parts = views.part_view(s, "right")
        self.assertEqual([c.item.label for c in parts.children], ["手", "胸", "屁股"])
        self.assertTrue(all(len(c.custom_id) <= 100 for c in view.children + parts.children))


class FakeMessage:
    def __init__(self, mid):
        self.id = mid

    async def edit(self, **kw):
        pass

    async def delete(self):
        pass


class FakeChannel:
    def __init__(self):
        self.id, self.category_id, self.sent, self._next, self.fail = CH, 9, [], 9000, False

    def get_partial_message(self, mid):
        return FakeMessage(mid)

    async def fetch_message(self, mid):
        return FakeMessage(mid)

    async def send(self, content=None, embed=None, view=None, allowed_mentions=None):
        if self.fail:
            raise discord.HTTPException(NS(status=500, reason="x"), "x")
        self.sent.append((content, embed.to_dict() if embed else None, allowed_mentions))
        self._next += 1
        return FakeMessage(self._next)


class FakeInteraction:
    def __init__(self, channel, uid, admin=False):
        self.user = NS(id=uid)
        self.channel, self.channel_id, self.guild_id = channel, channel.id, 1
        self.permissions = NS(administrator=admin, manage_guild=False, manage_messages=False)
        self.replies = []
        self.response = NS(defer=self._noop, send_message=self._send)
        self.followup = NS(send=self._send)
        self.views = []

    async def _noop(self, **kw):
        pass

    async def _send(self, content=None, view=None, **kw):
        self.replies.append(content)
        self.views.append(view)

    async def edit_original_response(self, content=None, view=None):
        self.replies.append(content)

    async def original_response(self):
        return FakeMessage(500)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.channel = FakeChannel()

        async def ready():
            pass
        self.bot = NS(get_channel=lambda cid: self.channel if cid == CH else None, wait_until_ready=ready)
        self.cfg = cfg_with(1.1)
        self.cfg["state_file"] = str(Path(self.tmp.name) / "state.json")
        self.svc = self.new_service()

    def new_service(self):
        return VolleyballService(self.bot, self.cfg, seed_factory=lambda: "seed", clock=lambda: NOW)

    async def asyncTearDown(self):
        self.svc.shutdown()
        self.tmp.cleanup()

    def it(self, uid, admin=False):
        return FakeInteraction(self.channel, uid, admin)

    async def lobby(self, n=4, target=None):
        await self.svc.create_game(self.it(1), target)
        s = next(iter(self.svc.sessions.values()))
        for uid in range(2, n + 1):
            await self.svc.handle_lobby(self.it(uid), s.id, "join")
        return s

    async def started(self, n=4):
        s = await self.lobby(n)
        await self.svc.handle_lobby(self.it(1), s.id, "start")
        return s

    def others(self, s):
        return engine.receivers(s) if s.phase == PHASE_RECEIVE else s.teams[1 - s.serving_team]

    async def test_lobby_limits(self):
        s = await self.lobby(10)
        it = self.it(11)
        await self.svc.handle_lobby(it, s.id, "join")
        self.assertIn("人数已满", it.replies[0])
        it = self.it(2)
        await self.svc.handle_lobby(it, s.id, "start")
        self.assertIn("只有主持人", it.replies[0])
        self.assertEqual(s.target_score, 12)

    async def test_needs_two_players(self):
        s = await self.lobby(1, target=15)
        self.assertEqual(s.target_score, 15)
        it = self.it(1)
        await self.svc.handle_lobby(it, s.id, "start")
        self.assertIn("至少需要 2", it.replies[0])
        await self.svc.handle_lobby(self.it(2), s.id, "join")
        await self.svc.handle_lobby(self.it(1), s.id, "start")
        self.assertEqual(s.status, Status.IN_PROGRESS)

    async def test_channel_busy_and_allow_list(self):
        await self.lobby(1)
        it = self.it(2)
        await self.svc.create_game(it)
        self.assertIn("已经有一局", it.replies[0])
        self.assertTrue(await self.svc.channel_allowed(self.channel))     # 默认不限频道
        self.svc.cfg["allowed_channel_ids"] = [1]
        self.assertFalse(await self.svc.channel_allowed(self.channel))
        self.svc.cfg["allowed_channel_ids"] = [CH]
        self.assertTrue(await self.svc.channel_allowed(self.channel))
        self.svc.cfg["allowed_channel_ids"], self.svc.cfg["allowed_category_ids"] = [], [9]
        self.assertTrue(await self.svc.channel_allowed(self.channel))

    async def test_only_server_can_serve(self):
        s = await self.started()
        wrong = next(u for u in s.participants if u != s.server_id)
        it = self.it(wrong)
        await self.svc.handle_serve(it, s.id, s.seq, "left")
        self.assertIn("不是你发球", it.replies[0])
        self.assertEqual(s.phase, PHASE_SERVE)
        await self.svc.handle_serve(self.it(s.server_id), s.id, s.seq, "left")
        self.assertEqual((s.phase, s.direction), (PHASE_RECEIVE, "left"))

    async def test_receive_all_resolves_and_secrecy(self):
        s = await self.started()
        team = 1 - s.serving_team
        await self.svc.handle_serve(self.it(s.server_id), s.id, s.seq, "left")
        a, b = engine.receivers(s)
        seq = s.seq
        await self.svc.handle_receive(self.it(a), s.id, seq, "chest", "left")
        it = self.it(a)
        await self.svc.handle_receive(it, s.id, seq, "hand", "right")      # 选了不能改
        self.assertIn("已经选过", it.replies[0])
        self.assertEqual(s.choices[str(a)], ["chest", "left"])
        outsider = self.it(s.server_id)
        await self.svc.handle_receive(outsider, s.id, seq, "hand", "left")
        self.assertIn("不是你接", outsider.replies[0])
        self.assertEqual(s.round_no, 0)
        await self.svc.handle_receive(self.it(b), s.id, seq, "hand", "right")
        self.assertEqual((s.round_no, s.scores[team]), (1, 3))
        self.assertEqual(s.phase, PHASE_SERVE)
        stale = self.it(a)
        await self.svc.handle_receive(stale, s.id, seq, "hand", "left")
        self.assertIn("已经结束", stale.replies[0])

    async def test_aim_opens_private_part_picker(self):
        s = await self.started()
        await self.svc.handle_serve(self.it(s.server_id), s.id, s.seq, "left")
        it = self.it(s.server_id)
        await self.svc.handle_aim(it, s.id, s.seq, "left")              # 发球方不能接
        self.assertIn("不是你接", it.replies[0])
        self.assertIsNone(it.views[0])
        a = engine.receivers(s)[0]
        it = self.it(a)
        await self.svc.handle_aim(it, s.id, s.seq, "right")
        self.assertIn("接右边", it.replies[0])
        self.assertEqual({c.direction for c in it.views[0].children}, {"right"})
        self.assertEqual(s.choices, {})                                 # 只选了方向还不算
        await self.svc.handle_receive(self.it(a), s.id, s.seq, "hand", "right")
        it = self.it(a)
        await self.svc.handle_aim(it, s.id, s.seq, "left")              # 选过了再点方向
        self.assertIn("已经选过", it.replies[0])

    async def test_timeouts(self):
        s = await self.started()
        before = s.seq
        await self.svc._timeout(s.id, Status.IN_PROGRESS, s.seq, NOW)      # 没发球：自动发
        self.assertEqual((s.phase, s.serve_auto), (PHASE_RECEIVE, True))
        self.assertEqual(s.seq, before + 1)
        await self.svc._timeout(s.id, Status.IN_PROGRESS, before, NOW)     # 旧计时器：无效
        self.assertEqual(s.round_no, 0)
        await self.svc._timeout(s.id, Status.IN_PROGRESS, s.seq, NOW)      # 没人接：结算
        self.assertEqual((s.round_no, s.idle_rounds, s.scores[s.serving_team]), (1, 1, 1))

    async def test_game_finishes_and_releases_channel(self):
        s = await self.started()
        s.target_score = 1
        await self.svc.handle_serve(self.it(s.server_id), s.id, s.seq, "left")
        for uid in engine.receivers(s):
            await self.svc.handle_receive(self.it(uid), s.id, s.seq, "hand", "left")
        self.assertEqual(s.status, Status.COMPLETED)
        self.assertNotIn(CH, self.svc.by_channel)
        self.assertIn("获胜", self.channel.sent[-1][1]["title"])

    async def test_cancel_permission(self):
        s = await self.started()
        it = self.it(2)
        await self.svc.handle_cancel(it, s.id)
        self.assertIn("只有主持人", it.replies[0])
        await self.svc.handle_cancel(self.it(99, admin=True), s.id)
        self.assertEqual(s.status, Status.CANCELLED)

    async def test_restart_keeps_game(self):
        s = await self.started()
        await self.svc.handle_serve(self.it(s.server_id), s.id, s.seq, "right")
        a = engine.receivers(s)[0]
        await self.svc.handle_receive(self.it(a), s.id, s.seq, "hand", "right")
        self.svc.shutdown()
        svc = self.new_service()
        await svc.restore()
        back = svc.sessions[s.id]
        self.assertEqual((back.direction, back.choices, back.phase), ("right", {str(a): ["hand", "right"]}, PHASE_RECEIVE))
        svc.shutdown()

    async def test_report_failure_is_kept_and_retried(self):
        s = await self.started()
        await self.svc.handle_serve(self.it(s.server_id), s.id, s.seq, "left")
        self.channel.fail = True
        for uid in engine.receivers(s):
            await self.svc.handle_receive(self.it(uid), s.id, s.seq, "hand", "left")
        self.assertEqual((s.round_no, len(s.pending_reports)), (1, 1))
        self.channel.fail = False
        await self.svc._flush_reports(s)
        self.assertEqual(s.pending_reports, [])

    async def test_ping_only_players(self):
        s = await self.started()
        content, _, allowed = self.channel.sent[-1]
        self.assertEqual(content, f"<@{s.server_id}>")
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)


if __name__ == "__main__":
    unittest.main()

"""超级滚雪球 规则测试：python -m unittest discover tests"""
import copy
import random
import unittest
from datetime import datetime, timezone

from minigames.snowball_relay import engine, renderer
from minigames.snowball_relay.models import TURN_DONE, TURN_SKIPPED, SnowballSession, Status

CFG = engine.load_config()
LINES = renderer.load_lines()


def cfg_with(capture=None):
    """capture=0 永不卷人；capture=1 必定卷人。"""
    c = copy.deepcopy(CFG)
    if capture is not None:
        c["capture"]["base_by_size"] = [capture] * 6
        c["capture"]["per_momentum"] = 0
        c["capture"]["max"] = capture
        for a in c["actions"].values():
            a["capture_mod"] = 0
            a["protection"] = 0
    return c


def new_session(n=3, seed="s", cfg=CFG):
    s = SnowballSession("sid", 1, 1, 1, 1, Status.IN_PROGRESS, datetime.now(timezone.utc),
                        participants=list(range(1, n + 1)))
    engine.start(s, seed, cfg)
    return s


def run_all(s, cfg, action="steady"):
    while s.current_actor_id is not None:
        engine.push(s, action, cfg)


class QueueTests(unittest.TestCase):
    def test_two_rounds_random_order(self):
        s = new_session(4)
        self.assertEqual(sorted(s.relay_order), [1, 2, 3, 4])
        self.assertEqual(s.turn_queue, s.relay_order * 2)
        self.assertEqual(new_session(4).relay_order, s.relay_order)          # 同种子同顺序
        orders = {tuple(new_session(4, seed=str(i)).relay_order) for i in range(30)}
        self.assertGreater(len(orders), 1)

    def test_max_pushes_without_capture(self):
        c = cfg_with(capture=0)
        for n, expected in ((3, 6), (6, 12)):
            s = new_session(n, cfg=c)
            run_all(s, c, "hard")
            self.assertEqual(s.action_sequence, expected)
            self.assertEqual(s.engulfed_user_ids, [])
            # 每轮每人恰好一次
            actors = [e["actor"] for e in s.action_log]
            self.assertEqual(actors[:n], s.relay_order)
            self.assertEqual(actors[n:], s.relay_order)

    def test_engulf_skips_remaining_turns_one_per_push(self):
        c = cfg_with(capture=1)
        s = new_session(3, cfg=c)
        a, b, cc = s.relay_order
        e1 = engine.push(s, "steady", c)
        self.assertEqual((e1["capture"]["target"], e1["capture"]["engulfed"]), (b, True))
        self.assertEqual(s.current_actor_id, cc)                    # B 两次机会都被跳过
        self.assertEqual([st for u, st in zip(s.turn_queue, s.turn_status) if u == b], [TURN_SKIPPED] * 2)
        e2 = engine.push(s, "steady", c)
        self.assertEqual(e2["capture"]["target"], a)                # 每棒只判定一人，目标是下一位等待者
        e3 = engine.push(s, "steady", c)
        self.assertIsNone(e3["capture"])                            # 只剩自己：不判定、不卷自己
        self.assertTrue(e3["finished"])
        self.assertEqual(s.engulfed_user_ids, [b, a])
        self.assertEqual(len(set(s.engulfed_user_ids)), len(s.engulfed_user_ids))
        for e in s.action_log:
            if e["capture"]:
                self.assertNotEqual(e["capture"]["target"], e["actor"])

    def test_example_from_spec(self):
        # A→B→C→A→B→C；A 首棒后 B 被卷入 → A(已用)→C→A→C
        c = cfg_with(capture=0)
        s = new_session(3, cfg=c)
        a, b, cc = s.relay_order
        s.size_level = 1
        c1 = cfg_with(capture=1)
        engine.push(s, "steady", c1)
        rest = []
        while s.current_actor_id is not None:
            rest.append(s.current_actor_id)
            engine.push(s, "steady", c)
        self.assertEqual(rest, [cc, a, cc])

    def test_last_push_no_candidate(self):
        c = cfg_with(capture=1)
        s = new_session(3, cfg=c)
        s.turn_status = [TURN_DONE] * 5 + ["pending"]
        s.turn_index = 5
        e = engine.push(s, "hard", c)
        self.assertIsNone(e["capture"])
        self.assertTrue(e["finished"])
        self.assertEqual(engine.end_kind(s), engine.END_NORMAL)


class ActionTests(unittest.TestCase):
    def fresh(self, size=1, momentum=0):
        s = new_session(3, cfg=cfg_with(capture=0))
        s.size_level, s.momentum = size, momentum
        return s

    def test_action_effects(self):
        c = cfg_with(capture=0)
        s = self.fresh(2, 2)
        engine.push(s, "steady", c)
        self.assertEqual((s.distance_m, s.size_level, s.momentum), (100, 3, 1))

        sizes = set()
        for i in range(200):
            s = new_session(3, seed=str(i), cfg=c)
            s.size_level, s.momentum = 2, 1
            engine.push(s, "hard", c)
            sizes.add(s.size_level)
            self.assertEqual((s.distance_m, s.momentum), (105, 2))
        self.assertEqual(sizes, {3, 4})

        sizes, hits = set(), 0
        for i in range(400):
            s = new_session(3, seed=str(i), cfg=c)
            s.size_level = 2
            e = engine.push(s, "aim", c)
            sizes.add(s.size_level)
            hits += e["hit"]
            self.assertEqual((s.distance_m, s.momentum), (95, 1))
        self.assertEqual(sizes, {2, 3})
        self.assertAlmostEqual(hits / 400, CFG["actions"]["aim"]["hit_chance"], delta=0.08)

        s = self.fresh(3, 3)
        e = engine.push(s, "repair", c)
        self.assertEqual((s.distance_m, s.size_level, s.momentum, e["debris"], e["hit"]), (80, 3, 1, 0, 0))

    def test_bounds(self):
        c = cfg_with(capture=0)
        for i in range(100):
            s = new_session(3, seed=str(i), cfg=c)
            s.size_level, s.momentum = 6, 3
            engine.push(s, "hard", c)                      # Lv.6 硬上限
            self.assertEqual((s.size_level, s.momentum), (6, 3))
        s = self.fresh(1, 0)
        engine.push(s, "repair", c)
        self.assertEqual(s.momentum, 0)

    def test_only_aim_hits_and_repair_has_no_debris(self):
        c = cfg_with(capture=0)
        for i in range(200):
            for action in ("steady", "hard", "repair"):
                s = new_session(3, seed=f"{action}{i}", cfg=c)
                e = engine.push(s, action, c)
                self.assertEqual(e["hit"], 0)
                if action == "repair":
                    self.assertEqual(e["debris"], 0)


class CaptureTests(unittest.TestCase):
    def test_formula_and_clamp(self):
        p = engine.capture_probability
        self.assertAlmostEqual(p(6, 3, "hard", CFG), 0.52)            # 25% + 3×6% + 9%
        self.assertAlmostEqual(p(6, 3, "aim", CFG), 0.46)
        self.assertAlmostEqual(p(6, 3, "steady", CFG), 0.38)
        self.assertAlmostEqual(p(6, 1, "repair", CFG), 0.03)          # 25% + 6% − 10% − 18%
        self.assertEqual(p(1, 0, "steady", CFG), 0)                   # 不低于 0
        c = copy.deepcopy(CFG)
        c["capture"]["per_momentum"] = 0.5
        self.assertEqual(p(6, 3, "hard", c), 0.65)                    # 不超过 65%
        for size, base in enumerate(CFG["capture"]["base_by_size"], start=1):
            self.assertAlmostEqual(p(size, 0, "aim", CFG), max(0, base + 0.03))

    def test_repair_protection_applies_to_this_handoff(self):
        s = new_session(3)
        s.size_level, s.momentum = 6, 3
        e = engine.push(s, "repair", CFG)
        self.assertAlmostEqual(e["capture"]["probability"], 0.03)


class ScoreTests(unittest.TestCase):
    def test_formula(self):
        s = new_session(4)
        s.distance_m, s.size_level, s.obstacle_count, s.successful_hits = 830, 5, 3, 2
        s.engulfed_user_ids = [2, 3]
        b = engine.score_breakdown(s, CFG)
        self.assertEqual(b["total"], 830 + 500 + 105 + 230 + 120)

    def test_size_counted_once_and_players_not_debris(self):
        c = cfg_with(capture=1)
        s = new_session(3, cfg=c)
        run_all(s, c, "hard")
        b = engine.score_breakdown(s, c)
        self.assertEqual(b["size"], s.size_level * 100)
        self.assertEqual(s.obstacle_count, sum(e["debris"] for e in s.action_log))
        self.assertEqual(b["engulfed"], len(s.engulfed_user_ids) * 60)
        self.assertTrue(all(v >= 0 and isinstance(v, int) for v in b.values()))


class ReplayAndReportTests(unittest.TestCase):
    def test_replay_is_deterministic(self):
        actions = ["hard", "aim", "steady", "repair", "aim", "hard", "aim", "steady"]
        a = engine.replay([1, 2, 3, 4], "seedX", actions, CFG)
        b = engine.replay([1, 2, 3, 4], "seedX", actions, CFG)
        self.assertEqual(a.action_log, b.action_log)
        self.assertEqual(engine.score_breakdown(a, CFG), engine.score_breakdown(b, CFG))

    def test_report_matches_result_and_is_pure(self):
        c = cfg_with(capture=1)
        s = new_session(4, cfg=c)
        e = engine.push(s, "aim", c)
        before = s.to_dict()
        text = renderer.report_embed(e, c, LINES, "flavor").description
        self.assertEqual(s.to_dict(), before)                               # 播报不改状态
        self.assertIn(f"<@{e['capture']['target']}>", text)
        self.assertIn(str(c["actions"]["aim"]["distance"]), text)
        self.assertIn(f"Lv.{e['after']['size_level']}", text)
        self.assertIn(f"<@{e['next_actor']}>", text)
        self.assertNotIn("@everyone", text)

    def test_all_engulfed_unreachable_under_base_rules(self):
        c = cfg_with(capture=1)
        for n in range(3, 7):
            s = new_session(n, cfg=c)
            run_all(s, c)
            self.assertEqual(engine.end_kind(s), engine.END_NORMAL)
            self.assertNotIn(s.action_log[-1]["actor"], s.engulfed_user_ids)


if __name__ == "__main__":
    unittest.main()

"""媚笙阁偷薯饼 规则纯逻辑测试：python -m unittest discover tests"""
import json
import random
import unittest
from datetime import datetime, timezone

from minigames.potato_heist import engine, renderer
from minigames.potato_heist.models import (
    CORRIDOR, DOWN, HALL, KITCHEN, STAY, STEAL, SUITES, UP, WAIT, HeistGame, Player, Role, Status, move, search,
)

MAX = 10
CAT = 1


def make_game(cat_pos=HALL, mice=None, secret="manxue", round_no=1):
    """mice: {user_id: position}；猫固定为 user 1。"""
    g = HeistGame("abc", 1, 1, 1, host_id=CAT, status=Status.ROUND_INPUT,
                  created_at=datetime.now(timezone.utc), round_no=round_no, secret_room=secret)
    g.players[CAT] = Player(CAT, "cat", role=Role.CAT, position=cat_pos)
    for uid, pos in (mice or {2: "zhenfeng", 3: "gehua"}).items():
        g.players[uid] = Player(uid, f"m{uid}", role=Role.MOUSE, position=pos)
    return g


def act(g, **actions):
    """act(g, p1=UP, p2=move(...))"""
    for key, a in actions.items():
        engine.submit_action(g, int(key[1:]), a)


def next_round(g):
    g.round_no += 1


class SetupTests(unittest.TestCase):
    def test_assign_roles(self):
        for seed in range(50):
            g = HeistGame("s", 1, 1, 1, 1, Status.ROLE_ASSIGNMENT, datetime.now(timezone.utc))
            for uid in range(1, 7):
                g.players[uid] = Player(uid, str(uid))
            engine.assign_roles(g, random.Random(seed))
            self.assertEqual(sum(p.role == Role.CAT for p in g.players.values()), 1)
            self.assertEqual(g.cat.position, HALL)
            self.assertIn(g.secret_room, SUITES)
            self.assertTrue(all(m.position in SUITES for m in g.mice()))


class LegalActionTests(unittest.TestCase):
    def test_cat_cannot_wait_and_always_has_action(self):
        g = make_game()
        self.assertEqual(engine.legal_actions(g, CAT), [UP, search(KITCHEN)])
        with self.assertRaises(engine.ActionError):
            engine.submit_action(g, CAT, WAIT)
        g.search_next_available_round[KITCHEN] = 99
        self.assertEqual(engine.legal_actions(g, CAT), [UP])
        g.players[CAT].position = CORRIDOR
        self.assertEqual(engine.legal_actions(g, CAT), [DOWN] + [search(s) for s in SUITES])

    def test_cat_cannot_use_passage_or_enter_rooms(self):
        g = make_game()
        for bad in (move(KITCHEN), move(CORRIDOR), move("manxue"), search("manxue"), STEAL):
            with self.assertRaises(engine.ActionError):
                engine.submit_action(g, CAT, bad)

    def test_mouse_map(self):
        g = make_game(mice={2: "zhenfeng", 3: HALL, 4: CORRIDOR})
        self.assertEqual(set(engine.legal_actions(g, 2)), {move(CORRIDOR), WAIT})
        # 公共区不能等待
        self.assertEqual(set(engine.legal_actions(g, 3)), {move(CORRIDOR), move(KITCHEN)})
        self.assertEqual(set(engine.legal_actions(g, 4)), {move(HALL), *(move(s) for s in SUITES)})
        # 不能跨越非相邻节点
        for bad in (move(KITCHEN), move(HALL), move("gehua")):
            with self.assertRaises(engine.ActionError):
                engine.submit_action(g, 2, bad)

    def test_secret_passage_both_ways_only_for_mice(self):
        g = make_game(mice={2: "manxue", 3: KITCHEN})
        self.assertIn(move(KITCHEN), engine.legal_actions(g, 2))
        self.assertIn(move("manxue"), engine.legal_actions(g, 3))
        self.assertIn(STEAL, engine.legal_actions(g, 3))
        self.assertNotIn(move(KITCHEN), engine.legal_actions(make_game(mice={2: "gehua"}), 2))

    def test_single_effective_action_overwrite(self):
        g = make_game()
        act(g, p2=WAIT)
        act(g, p2=move(CORRIDOR))
        self.assertEqual(g.players[2].action_for(1), move(CORRIDOR))

    def test_captured_mouse_cannot_act(self):
        g = make_game()
        g.players[2].captured = True
        self.assertEqual(engine.legal_actions(g, 2), [])
        with self.assertRaises(engine.ActionError):
            engine.submit_action(g, 2, WAIT)


class CooldownTests(unittest.TestCase):
    def test_round1_search_blocks_2_and_3(self):
        g = make_game(mice={2: "zhenfeng"})
        act(g, p1=search(KITCHEN), p2=WAIT)  # 搜空也冷却
        r = engine.resolve_round(g, MAX)
        self.assertEqual(r["search"]["captured"], [])
        for rnd, ok in ((2, False), (3, False), (4, True)):
            g.round_no = rnd
            self.assertEqual(search(KITCHEN) in engine.legal_actions(g, CAT), ok, rnd)


class CaptureTests(unittest.TestCase):
    def test_swap_does_not_capture(self):
        g = make_game(cat_pos=HALL, mice={2: CORRIDOR})
        act(g, p1=UP, p2=move(HALL))
        r = engine.resolve_round(g, MAX)
        self.assertEqual(r["captures"], [])

    def test_same_final_public_area_captures(self):
        g = make_game(cat_pos=HALL, mice={2: "zhenfeng", 3: KITCHEN})
        act(g, p1=UP, p2=move(CORRIDOR), p3=move(HALL))
        engine.resolve_round(g, MAX)
        self.assertTrue(g.players[2].captured)
        self.assertFalse(g.players[3].captured)

    def test_enter_searched_room_gets_caught(self):
        g = make_game(cat_pos=CORRIDOR, mice={2: "zhenfeng"})
        g.players[2].position = CORRIDOR
        g.players[CAT].position = HALL
        act(g, p1=UP, p2=move("gehua"))
        engine.resolve_round(g, MAX)
        self.assertFalse(g.players[2].captured)
        next_round(g)
        g.players[2].position = CORRIDOR
        act(g, p1=search("gehua"), p2=move("gehua"))
        engine.resolve_round(g, MAX)
        self.assertTrue(g.players[2].captured)

    def test_leave_searched_room_to_safe_place(self):
        g = make_game(cat_pos=HALL, mice={2: KITCHEN})
        act(g, p1=search(KITCHEN), p2=move("manxue"))  # 走密道离开
        engine.resolve_round(g, MAX)
        self.assertFalse(g.players[2].captured)

    def test_capture_before_steal(self):
        g = make_game(cat_pos=HALL, mice={2: KITCHEN, 3: "zhenfeng"})
        act(g, p1=search(KITCHEN), p2=STEAL, p3=WAIT)
        r = engine.resolve_round(g, MAX)
        self.assertTrue(g.players[2].captured)
        self.assertIsNone(g.potato_carrier_id)
        self.assertIsNone(r["potato_event"])


class PotatoTests(unittest.TestCase):
    def test_win_timeline(self):
        g = make_game(cat_pos=CORRIDOR, mice={2: KITCHEN, 3: "zhenfeng"})
        act(g, p1=DOWN, p2=STEAL, p3=WAIT)
        r = engine.resolve_round(g, MAX)                       # 第 1 轮：偷到
        self.assertEqual((r["potato_event"], r["winner"]), ("stolen", None))
        self.assertEqual(g.potato_carrier_id, 2)

        next_round(g)
        act(g, p1=UP, p2=move("manxue"), p3=WAIT)             # 第 2 轮：走密道离厨
        r = engine.resolve_round(g, MAX)
        self.assertIsNone(r["winner"])
        self.assertEqual(g.escaped_kitchen_at_round, 2)

        next_round(g)
        act(g, p1=DOWN, p2=WAIT, p3=WAIT)                     # 第 3 轮：存活 → 鼠胜
        r = engine.resolve_round(g, MAX)
        self.assertEqual((r["winner"], r["win_reason"]), ("mouse", engine.MICE_WIN_ESCAPED))

    def test_carrier_captured_returns_potato_not_same_round(self):
        g = make_game(cat_pos=HALL, mice={2: KITCHEN, 3: KITCHEN, 4: "zhenfeng"})
        g.potato_carrier_id = 2
        act(g, p1=search(KITCHEN), p2=WAIT, p4=WAIT)
        # 3 无法选择偷窃（薯饼被携带中）
        self.assertNotIn(STEAL, engine.legal_actions(g, 3))
        r = engine.resolve_round(g, MAX)
        self.assertEqual(r["potato_event"], "recovered")
        self.assertIsNone(g.potato_carrier_id)
        self.assertTrue(g.players[3].captured)

    def test_recovered_then_other_mouse_can_steal_later(self):
        g = make_game(cat_pos=CORRIDOR, mice={2: KITCHEN, 3: KITCHEN})
        g.potato_carrier_id = 2
        act(g, p1=DOWN, p2=move(HALL), p3=WAIT)               # 携带者在大堂撞上下楼的猫
        r = engine.resolve_round(g, MAX)
        self.assertTrue(g.players[2].captured)
        self.assertEqual((r["potato_event"], g.potato_carrier_id), ("recovered", None))
        next_round(g)
        act(g, p1=UP, p3=STEAL)
        r = engine.resolve_round(g, MAX)
        self.assertEqual((r["potato_event"], g.potato_carrier_id), ("stolen", 3))

    def test_simultaneous_steal_join_order(self):
        g = make_game(cat_pos=CORRIDOR, mice={4: KITCHEN, 2: KITCHEN, 3: KITCHEN})
        act(g, p1=DOWN, p2=STEAL, p3=STEAL, p4=STEAL)
        engine.resolve_round(g, MAX)
        self.assertEqual(g.potato_carrier_id, 4)  # 入局顺序第一个

    def test_round10_rules(self):
        g = make_game(cat_pos=CORRIDOR, mice={2: "zhenfeng"}, round_no=10)
        act(g, p1=DOWN, p2=WAIT)
        r = engine.resolve_round(g, MAX)
        self.assertEqual((r["winner"], r["win_reason"]), ("cat", engine.CAT_WIN_TIME_UP))

        g = make_game(cat_pos=CORRIDOR, mice={2: HALL}, round_no=10)
        g.potato_carrier_id, g.escaped_kitchen_at_round = 2, 9
        act(g, p1=search("gehua"), p2=move(KITCHEN))
        r = engine.resolve_round(g, MAX)
        self.assertEqual(r["winner"], "mouse")

    def test_all_captured_immediate_cat_win(self):
        g = make_game(cat_pos=HALL, mice={2: KITCHEN})
        act(g, p1=search(KITCHEN), p2=STEAL)
        r = engine.resolve_round(g, MAX)
        self.assertEqual((r["winner"], r["win_reason"]), ("cat", engine.CAT_WIN_ALL_CAPTURED))


class TimeoutTests(unittest.TestCase):
    def test_defaults(self):
        g = make_game(cat_pos=HALL, mice={2: CORRIDOR})
        r = engine.resolve_round(g, MAX)
        self.assertEqual(r["cat_action"], search(KITCHEN))
        self.assertTrue(r["cat_auto"])
        self.assertEqual(g.players[2].position, CORRIDOR)  # 原地不动
        self.assertIn("系统代选", g.players[CAT].auto_note)

    def test_cat_default_moves_when_all_cooling(self):
        g = make_game(cat_pos=HALL)
        g.search_next_available_round[KITCHEN] = 5
        self.assertEqual(engine.default_action(g, CAT), UP)
        self.assertEqual(engine.default_action(g, 2), STAY)

    def test_resolution_is_deterministic(self):
        def run():
            g = make_game(cat_pos=CORRIDOR, mice={2: KITCHEN, 3: KITCHEN, 4: "manxue"})
            act(g, p1=search("manxue"), p2=STEAL, p3=STEAL, p4=move(KITCHEN))
            result = engine.resolve_round(g, MAX)
            state = g.to_dict()
            state.pop("created_at")
            return result, state
        self.assertEqual(run(), run())


class InfoLeakTests(unittest.TestCase):
    SECRET_KEYS = {"position", "secret_room", "potato_carrier_id", "action", "carrying"}

    def _game(self):
        g = make_game(cat_pos=HALL, mice={2: KITCHEN, 3: "manxue"}, secret="manxue")
        act(g, p1=UP, p2=STEAL, p3=move(KITCHEN))
        result = engine.resolve_round(g, MAX)
        return g, result

    def test_public_view_has_no_secrets(self):
        g, result = self._game()
        pub = engine.public_view(g)
        self.assertFalse(self.SECRET_KEYS & set(pub))
        dumped = json.dumps(pub, default=str)
        self.assertNotIn("manxue", dumped)
        self.assertTrue(pub["potato_stolen"])

    def test_public_messages_have_no_secrets(self):
        g, result = self._game()
        pub = engine.public_view(g)
        texts = [renderer.panel_embed(pub, engine.load_config()).to_dict(), renderer.round_embed(result).to_dict()]
        blob = json.dumps(texts, ensure_ascii=False)
        for secret in ("幔雪阁", "后厨 ⇄", "<@2>", "<@3>"):
            self.assertNotIn(secret, blob)  # 未被捕的鼠不该在公共消息中以任何形式出现
        self.assertIn("薯饼失窃", blob)

    def test_cat_private_view_has_no_passage(self):
        g, _ = self._game()
        pv = engine.private_view(g, CAT)
        self.assertNotIn("secret_room", pv)
        self.assertNotIn("幔雪阁 ⇄", json.dumps(renderer.private_embed(pv, 2, MAX).to_dict(), ensure_ascii=False))

    def test_only_carrier_knows(self):
        g, _ = self._game()
        self.assertTrue(engine.private_view(g, 2)["carrying"])
        self.assertFalse(engine.private_view(g, 3)["carrying"])


if __name__ == "__main__":
    unittest.main()

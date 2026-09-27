"""千杯不醉 engine 单元测试：python -m unittest discover tests"""
import random
import unittest
from datetime import datetime, timezone

from minigames.drinking_contest import engine
from minigames.drinking_contest.models import Action, DrinkingGame, GameStatus, PlayerState, PlayerStatus

CFG = engine.load_config()
EVENTS = engine.load_events()


class ScriptedRandom(random.Random):
    """random() 按脚本返回，randint/choice 使用固定值，便于构造确定场景。"""

    def __init__(self, rolls, increase=10):
        super().__init__(0)
        self.rolls = list(rolls)
        self.increase = increase

    def random(self):
        return self.rolls.pop(0)

    def randint(self, a, b):
        return self.increase

    def choice(self, seq):
        return seq[0]


ALCOHOL, FOOD = 0.0, 0.95        # draw_once：< 0.9 为酒
PRETEND_OK, PRETEND_FAIL = 0.0, 0.5  # 作弊检定：< 0.4 为成功


def make_game(*drunks):
    game = DrinkingGame("g", 1, 1, 1, host_id=1, status=GameStatus.ACTIVE, round_number=1,
                        created_at=datetime.now(timezone.utc))
    for i, d in enumerate(drunks, start=1):
        game.players[i] = PlayerState(i, f"p{i}", drunkenness=d)
    return game


def submit(game, uid, action):
    p = game.players[uid]
    p.submitted_action, p.submitted_round = action, game.round_number


class DrawTests(unittest.TestCase):
    def test_distribution(self):
        rng = random.Random(42)
        draws = [engine.draw_once(rng, CFG, EVENTS) for _ in range(20000)]
        food = [d for d in draws if d["kind"] == "food"]
        self.assertAlmostEqual(len(food) / len(draws), 0.1, delta=0.01)
        self.assertTrue(all(d["delta"] == -5 for d in food))
        alcohol = {d["delta"] for d in draws if d["kind"] == "alcohol"}
        self.assertEqual(alcohol, set(range(5, 16)))

    def test_pretend_rate(self):
        rng = random.Random(7)
        p = PlayerState(1, "a")
        wins = sum(engine.resolve_player(p, Action.PRETEND, False, rng, CFG, EVENTS)["pretend_success"]
                   for _ in range(20000))
        self.assertAlmostEqual(wins / 20000, 0.4, delta=0.015)

    def test_floor_at_zero(self):
        draws, after = engine.drink_sequence(2, 1, ScriptedRandom([FOOD]), CFG, EVENTS)
        self.assertEqual(after, 0)

    def test_penalty_uses_three_normal_draws_including_food(self):
        rng = ScriptedRandom([PRETEND_FAIL, FOOD, FOOD, FOOD])
        r = engine.resolve_player(PlayerState(1, "a", drunkenness=50), Action.PRETEND, False, rng, CFG, EVENTS)
        self.assertEqual([d["kind"] for d in r["draws"]], ["food"] * 3)
        self.assertEqual(r["after"], 35)

    def test_penalty_stops_at_100(self):
        rng = ScriptedRandom([PRETEND_FAIL, ALCOHOL, ALCOHOL, ALCOHOL], increase=15)
        r = engine.resolve_player(PlayerState(1, "a", drunkenness=80), Action.PRETEND, False, rng, CFG, EVENTS)
        self.assertEqual(len(r["draws"]), 2)
        self.assertEqual(r["after"], 100)
        self.assertTrue(r["eliminated"])

    def test_pretend_success_unchanged(self):
        r = engine.resolve_player(PlayerState(1, "a", drunkenness=40), Action.PRETEND, False,
                                  ScriptedRandom([PRETEND_OK]), CFG, EVENTS)
        self.assertEqual(r["after"], 40)
        self.assertEqual(r["draws"], [])


class OutcomeTests(unittest.TestCase):
    def test_last_active_wins(self):
        game = make_game(10, 20)
        submit(game, 1, Action.DRINK)
        submit(game, 2, Action.WITHDRAW)
        res = engine.settle_round(game, ScriptedRandom([ALCOHOL]), CFG, EVENTS)
        self.assertEqual(res["outcome"], {"kind": engine.OUTCOME_WINNER, "winner_ids": [1]})
        self.assertEqual(game.players[1].status, PlayerStatus.WINNER)

    def test_continue_with_two_active(self):
        game = make_game(0, 0, 0)
        for uid in (1, 2, 3):
            submit(game, uid, Action.DRINK)
        res = engine.settle_round(game, ScriptedRandom([ALCOHOL] * 3), CFG, EVENTS)
        self.assertEqual(res["outcome"]["kind"], engine.OUTCOME_CONTINUE)

    def test_timeout_is_withdraw(self):
        game = make_game(30, 10, 0)
        submit(game, 3, Action.DRINK)
        res = engine.settle_round(game, ScriptedRandom([ALCOHOL]), CFG, EVENTS)
        self.assertTrue(res["players"][0]["timed_out"])
        self.assertEqual(game.players[1].status, PlayerStatus.WITHDRAWN)
        self.assertEqual(res["outcome"]["winner_ids"], [3])

    def test_all_down(self):
        game = make_game(95, 95)
        submit(game, 1, Action.DRINK)
        submit(game, 2, Action.DRINK)
        res = engine.settle_round(game, ScriptedRandom([ALCOHOL, ALCOHOL]), CFG, EVENTS)
        self.assertEqual(res["outcome"], {"kind": engine.OUTCOME_ALL_DOWN, "winner_ids": []})

    def test_all_withdraw_lowest_wins_with_ties(self):
        game = make_game(35, 55, 35)
        for uid in (1, 2, 3):
            submit(game, uid, Action.WITHDRAW)
        res = engine.settle_round(game, ScriptedRandom([]), CFG, EVENTS)
        self.assertEqual(res["outcome"], {"kind": engine.OUTCOME_TIEBREAK, "winner_ids": [1, 3]})

    def test_mixed_withdraw_and_eliminated(self):
        # 本轮醉倒者即使醉酒度更低（不可能），也不参与；只比较本轮退出者
        game = make_game(95, 60, 70)
        submit(game, 1, Action.DRINK)
        submit(game, 2, Action.WITHDRAW)
        submit(game, 3, Action.WITHDRAW)
        res = engine.settle_round(game, ScriptedRandom([ALCOHOL]), CFG, EVENTS)
        self.assertEqual(res["outcome"], {"kind": engine.OUTCOME_TIEBREAK, "winner_ids": [2]})

    def test_historical_withdrawals_excluded(self):
        game = make_game(0, 90, 90)
        game.players[1].status = PlayerStatus.WITHDRAWN
        game.players[1].withdrawn_round = 1
        game.round_number = 2
        submit(game, 2, Action.DRINK)
        submit(game, 3, Action.DRINK)
        res = engine.settle_round(game, ScriptedRandom([ALCOHOL, ALCOHOL]), CFG, EVENTS)
        self.assertEqual(res["outcome"]["kind"], engine.OUTCOME_ALL_DOWN)

    def test_previous_round_action_does_not_leak(self):
        game = make_game(0, 0)
        submit(game, 1, Action.DRINK)
        submit(game, 2, Action.DRINK)
        game.round_number = 2
        self.assertEqual(game.submitted_count(), 0)
        self.assertFalse(game.all_submitted())

    def test_serialization_roundtrip(self):
        game = make_game(10, 20)
        submit(game, 1, Action.PRETEND)
        game.round_deadline = datetime.now(timezone.utc)
        again = DrinkingGame.from_dict(game.to_dict())
        self.assertEqual(again.to_dict(), game.to_dict())


if __name__ == "__main__":
    unittest.main()

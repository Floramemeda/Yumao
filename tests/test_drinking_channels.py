"""千杯不醉 频道白名单单元测试：python -m unittest discover tests"""
import unittest
from types import SimpleNamespace

from minigames.drinking_contest import engine
from minigames.drinking_contest.service import channel_allowed

ALLOWED = engine.load_config()["allowed_channel_ids"]


class ChannelAllowedTests(unittest.TestCase):
    def test_allowed_channel(self):
        for cid in ALLOWED:
            self.assertTrue(channel_allowed(SimpleNamespace(id=cid), ALLOWED))

    def test_thread_under_allowed_channel(self):
        thread = SimpleNamespace(id=1, parent_id=ALLOWED[0])
        self.assertTrue(channel_allowed(thread, ALLOWED))

    def test_other_channel_and_thread(self):
        self.assertFalse(channel_allowed(SimpleNamespace(id=1), ALLOWED))
        self.assertFalse(channel_allowed(SimpleNamespace(id=1, parent_id=2), ALLOWED))

    def test_missing_channel(self):
        self.assertFalse(channel_allowed(None, ALLOWED))


if __name__ == "__main__":
    unittest.main()

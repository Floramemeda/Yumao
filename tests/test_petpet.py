"""踩奶测试：python -m unittest discover tests"""
import io
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import discord
from PIL import Image, ImageChops

from minigames.petpet.cog import COOLDOWN_SECONDS, PetPet, caption
from minigames.petpet.render import (ASSET_DIR, PHASE_OFFSET, SIZE, SOURCE_FRAMES, SPLIT_X, SUBFRAMES, durations_ms,
                                     make_gif, paw_layers, spring_track, subframe_cs)

BOT, A, B = 999, 1, 2


def image_bytes(size=(256, 256), fmt="PNG", frames=1, mode="RGB"):
    imgs = [Image.new(mode, size, (40 * i, 120, 200)) for i in range(frames)]
    buf = io.BytesIO()
    if frames > 1:
        imgs[0].save(buf, fmt, save_all=True, append_images=imgs[1:])
    else:
        imgs[0].save(buf, fmt)
    return buf.getvalue()


class RenderTests(unittest.TestCase):
    def check(self, data):
        g = Image.open(io.BytesIO(make_gif(data)))
        self.assertEqual((g.format, g.size, g.n_frames), ("GIF", (SIZE, SIZE), len(SOURCE_FRAMES) * SUBFRAMES))
        self.assertEqual(g.info.get("loop"), 0)
        g.seek(0)
        self.assertEqual(g.convert("RGBA").getpixel((1, SIZE - 1))[3], 0)   # 背景透明

    def test_inputs(self):
        self.check(image_bytes())
        self.check(image_bytes((300, 180)))                                # 非正方形
        self.check(image_bytes((64, 64), mode="RGBA"))
        self.check(image_bytes(fmt="GIF", frames=3))                       # 动态头像取第一帧

    def test_split_keeps_original_pixels(self):
        """左右两层拼回去和原关键帧逐像素一致（缝里没有像素，切开不裁到爪子）。"""
        for i, n in enumerate(SOURCE_FRAMES):
            im = Image.open(ASSET_DIR / f"paw_{i:02d}_sourceframe_{n:03d}.png").convert("RGBA")
            self.assertIsNone(im.getchannel("A").crop((SPLIT_X - 10, 0, SPLIT_X + 10, im.height)).getbbox())
            left = Image.new("RGBA", im.size, (0, 0, 0, 0))
            left.paste(im.crop((0, 0, SPLIT_X, im.height)), (0, 0))
            right = Image.new("RGBA", im.size, (0, 0, 0, 0))
            right.paste(im.crop((SPLIT_X, 0, im.width, im.height)), (SPLIT_X, 0))
            self.assertIsNone(ImageChops.difference(Image.alpha_composite(left, right), im).getbbox())

    def test_alternating_and_timing(self):
        layers = paw_layers()
        n = len(SOURCE_FRAMES)
        self.assertEqual(len(layers["left"]), n)
        self.assertEqual(len({p.size for p in layers["left"] + layers["right"]}), 1)   # 同一尺寸、同一原点
        pl = layers["press_l"]
        pr = [layers["press_r"][(i + PHASE_OFFSET) % n] for i in range(n)]
        for a, b in zip(pl, pr):                                               # 不会两只同时按到底
            self.assertFalse(a > 0.6 and b > 0.6)
        self.assertGreater(sum(abs(a - b) for a, b in zip(pl, pr)) / n, 0.6)   # 大体反相
        self.assertEqual(durations_ms()[:3], [111, 125, 125])                 # 帧号间隔 8 / 9 / 9 @24fps，三倍速
        g = Image.open(io.BytesIO(make_gif(image_bytes())))
        got = []
        for i in range(g.n_frames):
            g.seek(i)
            got.append(g.info["duration"])
        per_key = [sum(got[i * SUBFRAMES:(i + 1) * SUBFRAMES]) for i in range(n)]
        for a, b in zip(per_key, durations_ms()):                              # 子帧加起来 = 原关键帧时长（误差 < 10ms）
            self.assertLess(abs(a - b), 10)

    def test_spring_rebound_and_seamless_loop(self):
        layers = paw_layers()
        cs = subframe_cs()
        track = spring_track(list(layers["press_l"]), cs)
        self.assertEqual(len(track), len(SOURCE_FRAMES) * SUBFRAMES)
        self.assertLess(min(track), -0.05)                                    # 抬爪时反弹过头（往上鼓）
        self.assertGreater(max(track), 1.0)                                   # 按下时略微压过头
        self.assertGreater(min(track), -0.4)                                  # 只是一点点
        with mock.patch("minigames.petpet.render.SPRING_WARMUP", 10):          # 已是周期稳态：多跑几圈结果不变
            longer = spring_track(list(layers["press_l"]), cs)
        for a, b in zip(track, longer):
            self.assertAlmostEqual(a, b, places=4)

    def test_size_reasonable(self):
        self.assertLess(len(make_gif(image_bytes())), 200_000)

    def test_garbage_raises(self):
        with self.assertRaises(Exception):
            make_gif(b"not an image")


class FakeAsset:
    def __init__(self, data=None, fail=False):
        self.data, self.fail = data, fail

    def replace(self, **kw):
        return self

    async def read(self):
        if self.fail:
            raise discord.HTTPException(NS(status=500, reason="x"), "x")
        return self.data


class FakeInteraction:
    def __init__(self, uid):
        self.user = NS(id=uid)
        self.sent, self.deferred, self.ephemeral = [], False, []

        async def defer(**kw):
            self.deferred = True

        async def send_message(text, ephemeral=False):
            self.ephemeral.append(text)

        async def followup(content=None, file=None, allowed_mentions=None, **kw):
            self.sent.append((content, file, allowed_mentions))
        self.response = NS(defer=defer, send_message=send_message)
        self.followup = NS(send=followup)


class CogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = [100.0]
        self.cog = PetPet(NS(user=NS(id=BOT)), clock=lambda: self.now[0])

    async def pet(self, actor, target, avatar=None, fail=False):
        it = FakeInteraction(actor)
        await self.cog.pet(it, NS(id=target, display_avatar=FakeAsset(avatar or image_bytes(), fail)))
        return it

    async def test_sends_gif_without_pinging(self):
        it = await self.pet(A, B)
        text, file, allowed = it.sent[0]
        self.assertEqual(text, f"🐾 玉猫应 <@{A}> 的要求，给 <@{B}> 踩了踩奶～")
        self.assertEqual(file.filename, "petpet.gif")
        self.assertFalse(allowed.users or allowed.roles or allowed.everyone)

    async def test_cooldown(self):
        await self.pet(A, B)
        it = await self.pet(A, B)
        self.assertEqual(it.sent, [])
        self.assertIn("秒后再踩", it.ephemeral[0])
        self.assertTrue((await self.pet(B, A)).sent)                       # 别人不受影响
        self.now[0] += COOLDOWN_SECONDS
        self.assertTrue((await self.pet(A, B)).sent)

    async def test_avatar_failures(self):
        it = await self.pet(A, B, fail=True)
        self.assertIn("没拿到头像", it.sent[0][0])
        self.now[0] += COOLDOWN_SECONDS
        it = await self.pet(A, B, avatar=b"garbage")
        self.assertIn("没拿到头像", it.sent[0][0])

    def test_captions(self):
        self.assertIn("呼噜呼噜", caption(A, A, BOT))
        self.assertIn("玉猫给自己踩了踩奶", caption(A, BOT, BOT))


if __name__ == "__main__":
    unittest.main()

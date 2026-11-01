"""踩奶测试：python -m unittest discover tests"""
import io
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import discord
from PIL import Image, ImageChops

from minigames.petpet import anim, poke, prpr, squish
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

    async def pet(self, actor, target, avatar=None, fail=False, kind="knead"):
        it = FakeInteraction(actor)
        await self.cog.pet(it, NS(id=target, display_avatar=FakeAsset(avatar or image_bytes(), fail)), kind)
        return it

    async def test_sends_gif_without_pinging(self):
        it = await self.pet(A, B)
        text, file, allowed = it.sent[0]
        self.assertEqual(text, f"🐾 玉猫应 <@{A}> 的要求，给 <@{B}> 踩了踩奶～")
        self.assertEqual(file.filename, "knead.gif")
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

    async def test_poke(self):
        it = await self.pet(A, B, kind="poke")
        text, file, allowed = it.sent[0]
        self.assertEqual(text, f"🐾 玉猫应 <@{A}> 的要求，戳了戳 <@{B}>～")
        self.assertEqual(file.filename, "poke.gif")
        self.assertFalse(allowed.users or allowed.roles or allowed.everyone)
        it = await self.pet(A, B, kind="knead")                              # 两种动作共用冷却
        self.assertIn("秒后再踩", it.ephemeral[0])

    def test_captions(self):
        self.assertIn("呼噜呼噜", caption(A, A, BOT))
        self.assertIn("玉猫给自己踩了踩奶", caption(A, BOT, BOT))
        self.assertIn("戳一下软一下", caption(A, A, BOT, "poke"))
        self.assertIn("玉猫戳了戳自己", caption(A, BOT, BOT, "poke"))
        self.assertIn("拍扁了，啪叽", caption(A, A, BOT, "squish"))
        self.assertIn("把自己拍扁了", caption(A, BOT, BOT, "squish"))

    async def test_squish(self):
        it = await self.pet(A, B, kind="squish")
        text, file, _ = it.sent[0]
        self.assertEqual(text, f"🐾 玉猫应 <@{A}> 的要求，一爪把 <@{B}> 拍扁了，啪叽！")
        self.assertEqual(file.filename, "squish.gif")

    async def test_prpr(self):
        it = await self.pet(A, B, kind="prpr")
        text, file, _ = it.sent[0]
        self.assertEqual(text, f"😛 玉猫应 <@{A}> 的要求，把 <@{B}> 舔了个遍，prprprpr！")
        self.assertEqual(file.filename, "prpr.gif")

    def test_menus(self):
        self.assertEqual([m.name for m in self.cog.menus], ["踩奶", "戳戳", "拍扁", "舔舔"])


class PokeRenderTests(unittest.TestCase):
    def test_output(self):
        g = Image.open(io.BytesIO(poke.make_gif(image_bytes())))
        self.assertEqual((g.format, g.size, g.n_frames),
                         ("GIF", (poke.SIZE, poke.SIZE), len(poke.SOURCE_FRAMES) * poke.SUBFRAMES))
        self.assertEqual(g.info.get("loop"), 0)
        self.assertEqual(g.convert("RGBA").getpixel((1, poke.SIZE - 1))[3], 0)   # 背景透明
        got = []
        for i in range(g.n_frames):
            g.seek(i)
            got.append(g.info["duration"])
        self.assertTrue(all(d >= 20 for d in got))                             # 浏览器会把 <20ms 的帧拖成 100ms
        self.assertTrue(1500 <= sum(got) <= 2500)                              # 2× 速，约 2.1 秒一圈
        self.assertLess(len(poke.make_gif(image_bytes((64, 64), mode="RGBA"))), 300_000)

    def test_keyframes_and_contact(self):
        paws, contact = poke.paw_frames()
        self.assertEqual(len(paws), 16)
        self.assertEqual(len({p.size for p in paws}), 1)                       # 整张贴、不单独裁切
        self.assertEqual(max(contact), 1.0)
        self.assertEqual(contact[0], 0.0)                                      # 首尾都没碰到：循环衔接自然
        self.assertEqual(contact[-1], 0.0)
        self.assertGreater(sum(1 for c in contact if c > 0.5), 3)              # 戳了两下
        self.assertLessEqual(poke.PAW_OFFSET[1], 0)                            # 手臂断口不会露出来
        self.assertGreaterEqual(poke.PAW_OFFSET[0] + poke.SOURCE, poke.CANVAS)

    def test_rebound_and_seamless(self):
        cs = poke.subframe_cs()
        args = (poke.SPRING_HZ * poke.SPEED, poke.SPRING_DAMPING)
        track = anim.spring_track(list(poke.paw_frames()[1]), cs, *args, poke.SPRING_WARMUP)
        self.assertLess(min(track), -0.1)                                     # 收爪后往右上方过冲
        longer = anim.spring_track(list(poke.paw_frames()[1]), cs, *args, poke.SPRING_WARMUP + 4)
        for a, b in zip(track, longer):
            self.assertAlmostEqual(a, b, places=4)


class SquishRenderTests(unittest.TestCase):
    def test_output(self):
        g = Image.open(io.BytesIO(squish.make_gif(image_bytes())))
        self.assertEqual((g.format, g.size), ("GIF", (160, 160)))
        self.assertEqual(len(squish.frames(image_bytes())), len(squish.TIMELINE))
        self.assertEqual(g.info.get("loop"), 0)
        self.assertEqual(g.convert("RGBA").getpixel((1, 159))[3], 0)            # 背景透明
        got = []
        for i in range(g.n_frames):
            g.seek(i)
            got.append(g.info["duration"])
        self.assertEqual(sum(got), 1650)                                       # 相同的相邻帧会被合并，总时长不变
        self.assertLess(len(squish.make_gif(image_bytes((64, 64), mode="RGBA"))), 300_000)

    def test_keyframes(self):
        paws = squish.paw_frames()
        self.assertEqual(len(paws), 20)
        self.assertEqual(len({p.size for p in paws}), 1)                       # 整张贴、不单独裁切
        self.assertEqual(squish.TIMELINE[0], squish.TIMELINE[-1])               # 首尾同一姿势

    def test_flatten_exactly_on_contact(self):
        h = squish.heights()
        for key, height in zip(squish.TIMELINE, h):
            self.assertEqual(key in squish.CONTACT, height == squish.FLAT)     # 爪子压着 ⇔ 压扁
        first = next(f for f, k in enumerate(squish.TIMELINE) if k in squish.CONTACT)
        self.assertTrue(all(x == 1.0 for x in h[:first]))                      # 拍到之前不变形
        flat_ms = sum(ms for x, ms in zip(h, squish.FRAME_MS) if x == squish.FLAT)
        self.assertEqual(flat_ms, 120 + 400)                                   # 拍下 + 压扁停留
        self.assertAlmostEqual(max(h), squish.STRETCH, places=2)               # 回弹拉到 110%
        self.assertAlmostEqual(h[-1], 1.0, delta=0.002)                         # 结尾复位：循环无缝
        _, release_end = squish.phase_ms("release")
        peak_t = sum(squish.FRAME_MS[:h.index(max(h))])
        self.assertTrue(release_end <= peak_t < squish.phase_ms("rebound")[1])  # 110% 落在回弹段

    def test_phases(self):
        lengths = {name: sum(ms for _, ms in part) for name, part in squish.PHASES}
        self.assertEqual(lengths, {"anticipation": 250, "strike": 120, "hold": 400,
                                   "release": 250, "rebound": 350, "settle": 280})
        self.assertTrue(all(ms >= 40 for ms in squish.FRAME_MS))                # 浏览器会把 <20ms 的帧拖成 100ms
        strike = [k for k, _ in dict(squish.PHASES)["strike"]]
        self.assertEqual(strike[0], 5)                                         # 拍下段第一帧就是接触帧
        self.assertAlmostEqual(squish.width_for(squish.FLAT), 1.2)

    def test_paw_flips_only_at_contact(self):
        # 肉垫朝前（0~4、14~19）和爪背（5~13）只在拍到 / 抬起时切换
        backs = [k in range(5, 14) for k in squish.TIMELINE]
        contact = [k in squish.CONTACT for k in squish.TIMELINE]
        self.assertEqual(backs, contact)


class PrprRenderTests(unittest.TestCase):
    def test_output(self):
        g = Image.open(io.BytesIO(prpr.make_gif(image_bytes())))
        self.assertEqual((g.format, g.size, g.info.get("loop")), ("GIF", (160, 160), 0))
        self.assertEqual(g.convert("RGBA").getpixel((1, 159))[3], 0)            # 背景透明
        got = []
        for i in range(g.n_frames):
            g.seek(i)
            got.append(g.info["duration"])
        self.assertTrue(1200 <= sum(got) <= 1500)
        self.assertTrue(all(d >= 40 for d in got))                             # 不超过 25fps
        self.assertLess(len(prpr.make_gif(image_bytes((64, 64), mode="RGBA"))), 300_000)

    def test_keyframes_and_contact(self):
        cats, contact = prpr.cat_frames()
        self.assertEqual(len(cats), len(prpr.TIMELINE))
        self.assertEqual(len({c.size for c in cats}), 1)                        # 整张贴、不单独裁切
        self.assertNotIn(0, [k for k, _ in prpr.TIMELINE])                     # 开头推镜那帧不用
        licks = [c for c, _, _ in contact]
        self.assertEqual(max(licks), 1.0)
        self.assertGreaterEqual(sum(1 for c in licks if c > 0.5), 10)          # 疯狂舔
        self.assertGreaterEqual(sum(1 for c in licks if c == 0), 4)            # 中间有缩回去，有节奏
        self.assertTrue(all(dx <= 0 for c, dx, _ in contact if c > 0.5))       # 舌头从右边来，头像往左被顶开

    def test_wobble_and_seamless(self):
        cs = prpr.subframe_cs()
        targets = [c * dx for c, dx, _ in prpr.cat_frames()[1]]
        args = (prpr.SPRING_HZ, prpr.SPRING_DAMPING)
        track = anim.spring_track(targets, cs, *args, prpr.SPRING_WARMUP)
        self.assertLess(min(track), -0.4)                                      # 舔到时明显被顶开
        longer = anim.spring_track(targets, cs, *args, prpr.SPRING_WARMUP + 4)
        for a, b in zip(track, longer):
            self.assertAlmostEqual(a, b, places=3)


if __name__ == "__main__":
    unittest.main()

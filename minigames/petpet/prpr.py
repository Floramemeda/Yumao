"""舔舔 GIF：用 assets/prpr/ 里提供的白猫舔舔关键帧（视频抽帧，透明 PNG）舔在头像上。

猫和舌头不重画、不单独裁切或居中：关键帧都是同一块 480×480 画布（manifest.json），原样贴在右下角，相对对齐不变。
头像是圆形，放在画面左边、垫在猫下面；舌头和口水画在头像上面。
素材的舌头大约每 3 帧一轮：缩回嘴边 → 甩到高处 → 卷过来 / 横扫到左边。按 TIMELINE 重新排时长：
缩回那一下只停 40ms，舔到的姿势停 80ms，约 1.24 秒一圈、25fps，比原视频（4 秒）快得多。
素材第 0 帧是视频开头的推镜（猫偏小），不用；第 17 帧和第 16 帧几乎一样，也去掉。
舌头每次舔到头像（舌头的粉色像素落在头像圆里），头像就被往舔的反方向顶开一点、带一点歪斜；
用欠阻尼弹簧跟随，每张关键帧拆成 40ms 的子帧给头像晃动用，弹簧取周期稳态保证首尾无缝。
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image

from . import anim

ASSET_DIR = Path(__file__).parent / "assets" / "prpr"
SOURCE = 480                    # 素材画布边长（也是合成画布）
# (关键帧序号, 毫秒)：1、4、7、10、18 是舌头缩回嘴边，其他是舔到头像的姿势
TIMELINE = [(1, 40), (2, 80), (3, 80), (4, 40), (5, 80), (6, 80), (7, 40), (8, 80), (9, 80),
            (10, 40), (11, 80), (12, 80), (13, 80), (14, 80), (15, 80), (16, 80), (18, 40), (19, 80)]
FRAME_MS = 40                   # 子帧时长（25fps）

SIZE = 160                      # 输出边长
WORK = SIZE * 2                 # 合成时的工作尺寸
AVATAR_D = 290                  # 头像直径（素材坐标，输出约 97 像素）
AVATAR_CENTER = (150, 228)      # 头像中心（素材坐标）：左边，舌头甩过来正好盖住头像的右下半张脸
PUSH_PX = 3                     # 舔得最狠时头像被顶开几像素（输出像素，算上弹簧过冲约 2~4）
TILT = 4                        # 舔得最狠时头像歪几度
SPRING_HZ = 10                  # 够快：头像在舔到的那一帧就动，不拖拍
SPRING_DAMPING = 0.35           # 欠阻尼：被舔开后会晃回来、略微过头
SPRING_WARMUP = 4


def subframe_cs() -> list[list[int]]:
    return [[FRAME_MS // 10] * (ms // FRAME_MS) for _, ms in TIMELINE]


def _is_tongue(r: int, g: int, b: int, a: int) -> bool:
    return a > 128 and r > 200 and g < 150 and b < 170 and r - g > 70


@lru_cache(maxsize=1)
def cat_frames() -> tuple[tuple[Image.Image, ...], tuple[tuple[float, float, float], ...]]:
    """(缩到工作尺寸的关键帧, 每帧 (舔到程度 0~1, 被顶开的方向 x, y))，按 TIMELINE 的顺序。

    舔到程度 = 头像圆里舌头像素的多少；方向 = 从舌头落点指向头像中心（单位向量）。"""
    cx, cy = AVATAR_CENTER
    r2 = (AVATAR_D / 2) ** 2
    frames, raw = [], []
    for key, _ in TIMELINE:
        im = Image.open(ASSET_DIR / f"prpr_{key:02d}.png").convert("RGBA")
        px = im.load()
        n = sx = sy = 0
        for y in range(0, SOURCE, 2):
            for x in range(0, SOURCE, 2):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r2 and _is_tongue(*px[x, y]):
                    n, sx, sy = n + 1, sx + x, sy + y
        if n:
            dx, dy = cx - sx / n, cy - sy / n
            length = math.hypot(dx, dy) or 1.0
            raw.append((n, dx / length, dy / length))
        else:
            raw.append((0, 0.0, 0.0))
        frames.append(im.resize((WORK, WORK), Image.LANCZOS))
    peak = max(n for n, _, _ in raw)
    return tuple(frames), tuple((n / peak, dx, dy) for n, dx, dy in raw)


def frames(avatar: bytes) -> list[Image.Image]:
    k = WORK / SOURCE
    d = round(AVATAR_D * k)
    disc = anim.avatar_disc(avatar, d)
    cats, contact = cat_frames()
    cs = subframe_cs()
    args = (cs, SPRING_HZ, SPRING_DAMPING, SPRING_WARMUP)
    push = PUSH_PX * WORK / SIZE
    track_x = anim.spring_track([c * dx for c, dx, _ in contact], *args)
    track_y = anim.spring_track([c * dy for c, _, dy in contact], *args)
    keys = [i for i, parts in enumerate(cs) for _ in parts]
    out = []
    for i, x, y in zip(keys, track_x, track_y):
        canvas = Image.new("RGBA", (WORK, WORK), (0, 0, 0, 0))
        body = disc.rotate(TILT * x, resample=Image.BICUBIC, expand=True)   # 往左顶开时逆时针歪
        c = (AVATAR_CENTER[0] * k + push * x, AVATAR_CENTER[1] * k + push * y)
        canvas.alpha_composite(body, (round(c[0] - body.width / 2), round(c[1] - body.height / 2)))
        canvas.alpha_composite(cats[i])
        out.append(anim.finish_frame(canvas, SIZE))
    return out


def make_gif(avatar: bytes) -> bytes:
    """返回 GIF 字节。头像无法识别时抛 PIL.UnidentifiedImageError。"""
    return anim.save_gif(frames(avatar), subframe_cs())

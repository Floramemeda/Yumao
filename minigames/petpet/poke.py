"""戳戳 GIF：用 assets/poke/ 里提供的猫爪戳戳关键帧（视频抽帧，透明 PNG）叠在头像上。

猫爪不重画、不单独裁切或居中：16 张关键帧都是 480×480，按原顺序播放，每张都以同一个偏移 PAW_OFFSET
贴到 CANVAS×CANVAS 的画布上（所有帧偏移相同，相对对齐不变）。偏移向上移，是为了让爪垫落到头像右上方；
手臂本来就从画面上沿、右沿伸出，移出画布的只是手臂。
每帧时长按原视频帧号间隔换算（24.149fps），再 SPEED 倍速。
头像是圆形、占画面约 2/3，戳到最深时爪垫按在头像右上方。猫爪最低点越过 CONTACT_Y 就算戳到：
头像被往左下方推开 PUSH_PX 像素，同时沿「右上 → 左下」方向压扁；收爪时用欠阻尼弹簧往右上方弹回、略微过冲再复位。
与踩奶相同，每张关键帧拆成 SUBFRAMES 个子帧给头像回弹用，弹簧取周期稳态保证首尾无缝。
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image

from . import anim

ASSET_DIR = Path(__file__).parent / "assets" / "poke"
SOURCE_FRAMES = [0, 6, 12, 19, 25, 32, 38, 44, 51, 57, 64, 70, 76, 83, 89, 96]
SOURCE_FPS = 24.149
LAST_GAP = 6               # 最后一帧回到第一帧的间隔（帧），沿用抽帧间隔
SPEED = 2                  # 原视频约 4 秒 → 约 2.1 秒一个循环

SOURCE = 480               # 原素材画布边长
CANVAS = 480               # 合成画布边长（以下坐标都在这个画布上）
PAW_OFFSET = (40, -70)     # 每张猫爪关键帧贴到画布上的位置（所有帧相同；x ≥ 0、y ≤ 0，手臂断口不会露出来）
SIZE = 160                 # 输出边长
WORK = SIZE * 2            # 合成时的工作尺寸

AVATAR_D = 315             # 头像直径（输出约 105 像素）
AVATAR_CENTER = (222, 308) # 戳到最深时爪垫（原素材约 (260, 300)）按在头像右上方
CONTACT_Y = 295            # 猫爪最低点（原素材坐标）超过这里算戳到头像
PUSH_PX = 5                # 戳到最深时头像往左下方移动几像素（输出像素）
SQUASH = 0.1               # 戳到最深时沿戳的方向压缩的比例
SUBFRAMES = 3
SPRING_HZ = 3.2            # 换算到原速的弹簧频率（实际 × SPEED）
SPRING_DAMPING = 0.28      # 欠阻尼：收爪后往右上方弹过头一点再复位
SPRING_WARMUP = 4


def durations_ms() -> list[int]:
    return anim.keyframe_ms(SOURCE_FRAMES, SOURCE_FPS, LAST_GAP, SPEED)


def subframe_cs() -> list[list[int]]:
    return anim.split_cs(durations_ms(), SUBFRAMES)


@lru_cache(maxsize=1)
def paw_frames() -> tuple[tuple[Image.Image, ...], tuple[float, ...]]:
    """(缩到工作尺寸的猫爪帧, 每帧接触深度 0~1)。接触深度 = 猫爪最低点越过 CONTACT_Y 的程度。"""
    k = WORK / CANVAS
    frames, bottoms = [], []
    for i, n in enumerate(SOURCE_FRAMES):
        im = Image.open(ASSET_DIR / f"poke_{i:02d}_source_{n:03d}.png").convert("RGBA")
        bottoms.append(im.getchannel("A").getbbox()[3])
        frames.append(im.resize((round(SOURCE * k), round(SOURCE * k)), Image.LANCZOS))
    deepest = max(bottoms)
    contact = tuple(max(0.0, (b - CONTACT_Y) / (deepest - CONTACT_Y)) for b in bottoms)
    return tuple(frames), contact


def _squash_toward_lower_left(layer: Image.Image, center: tuple[float, float], r: float, s: float) -> Image.Image:
    """沿「右上 → 左下」方向把头像压缩 s（负值 = 往右上拉长），垂直方向放大 s/2，以左下边缘为固定点。"""
    cx, cy = (math.sqrt(0.5), -math.sqrt(0.5))          # 指向右上（y 向下）
    qx, qy = (math.sqrt(0.5), math.sqrt(0.5))           # 与之垂直
    ax, ay = center[0] - r * cx, center[1] - r * cy     # 左下边缘：固定点
    a, b = 1 / (1 - s), 1 / (1 + s / 2)                 # 逆变换的两个缩放
    m11 = a * cx * cx + b * qx * qx
    m12 = a * cx * cy + b * qx * qy
    m22 = a * cy * cy + b * qy * qy
    # 输出点 p' → 输入点 p = A + M⁻¹ (p' − A)
    return layer.transform(layer.size, Image.AFFINE,
                           (m11, m12, ax - m11 * ax - m12 * ay, m12, m22, ay - m12 * ax - m22 * ay),
                           resample=Image.BICUBIC)


def frames(avatar: bytes) -> list[Image.Image]:
    k = WORK / CANVAS
    d = round(AVATAR_D * k)
    center = (AVATAR_CENTER[0] * k, AVATAR_CENTER[1] * k)
    disc = anim.avatar_disc(avatar, d)
    paws, contact = paw_frames()
    cs = subframe_cs()
    track = anim.spring_track(list(contact), cs, SPRING_HZ * SPEED, SPRING_DAMPING, SPRING_WARMUP)
    paw_at = (round(PAW_OFFSET[0] * k), round(PAW_OFFSET[1] * k))
    push = PUSH_PX * WORK / SIZE / math.sqrt(2)          # 左下方向的 x、y 分量
    out = []
    for f, x in enumerate(track):
        c = (center[0] - push * x, center[1] + push * x)  # 戳到时往左下推，回弹时往右上弹
        layer = Image.new("RGBA", (WORK, WORK), (0, 0, 0, 0))
        layer.alpha_composite(disc, (round(c[0] - d / 2), round(c[1] - d / 2)))
        canvas = _squash_toward_lower_left(layer, c, d / 2, SQUASH * x)
        # alpha_composite 不接受负坐标：负的部分从贴图里裁掉
        canvas.alpha_composite(paws[f // SUBFRAMES], (max(0, paw_at[0]), max(0, paw_at[1])),
                               (max(0, -paw_at[0]), max(0, -paw_at[1])))
        out.append(anim.finish_frame(canvas, SIZE))
    return out


def make_gif(avatar: bytes) -> bytes:
    """返回 GIF 字节。头像无法识别时抛 PIL.UnidentifiedImageError。"""
    return anim.save_gif(frames(avatar), subframe_cs())

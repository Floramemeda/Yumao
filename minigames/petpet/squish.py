"""拍扁 GIF：用 assets/squish/ 里提供的猫爪拍扁关键帧（视频抽帧，透明 PNG）拍在头像上。

猫爪不重画、不单独裁切或居中：20 张关键帧都是同一块 582×806 画布（manifest.json），每张都以同一个偏移贴到
合成画布上，相对对齐不变。素材里抬起时是粉色肉垫朝前，拍下去后是蜷起来的爪背；肉垫 → 爪背的切换只放在
接触那一帧（素材第 5 张，自带速度线），所以翻面和「拍到」是同一瞬间，不会在半空突然翻过来。
时长不沿用原视频（4 秒太慢），按 PHASES 分段重排，约 1.65 秒一圈，各段时长不对称：
蓄力 250ms → 一下拍下 120ms → 压扁停留 400ms → 抬爪 250ms → 头像弹性回弹 350ms → 回到悬停 280ms
（首尾是同一姿势，循环无缝）。帧大多是 40ms（25fps），个别 50ms 用来凑够每段时长。
头像在猫爪下方，底边固定：拍到那一帧瞬间压成 40% 高、120% 宽，爪子压着的整段都保持扁的；
抬爪时用欠阻尼弹簧慢慢鼓回来，抬爪结束时刚好回到 100%，接着冲到 110% 高、再回落；回到悬停那段把余振收干净。
宽度跟着高度反向变化（压扁变宽、拉长变窄），看起来像软的。
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image

from . import anim

ASSET_DIR = Path(__file__).parent / "assets" / "squish"
SOURCE_FRAMES = [0, 8, 16, 22, 25, 27, 29, 31, 33, 36, 42, 50, 58, 64, 68, 70, 72, 75, 82, 90]
SOURCE_W, SOURCE_H = 582, 806   # 素材画布

# 分段：(段名, [(关键帧序号, 毫秒), ...])。5 = 拍到（爪背 + 速度线），5~13 爪子压着，14 起抬爪、肉垫朝前
PHASES = [
    ("anticipation", [(0, 40), (1, 40), (2, 40), (3, 40), (4, 40), (4, 50)]),
    ("strike", [(5, 40), (6, 40), (7, 40)]),
    ("hold", [(8, 40), (8, 40), (9, 40), (9, 40), (10, 40), (10, 40), (11, 40), (11, 40), (12, 40), (13, 40)]),
    ("release", [(14, 40), (14, 40), (15, 40), (15, 40), (16, 40), (16, 50)]),
    ("rebound", [(17, 40), (17, 40), (17, 40), (18, 40), (18, 40), (18, 50), (18, 50), (18, 50)]),
    ("settle", [(19, 40), (19, 40), (19, 40), (19, 40), (0, 40), (0, 40), (0, 40)]),
]
TIMELINE = [key for _, part in PHASES for key, _ in part]   # 每个输出帧用哪张关键帧
FRAME_MS = [ms for _, part in PHASES for _, ms in part]
CONTACT = range(5, 14)          # 这些关键帧里爪子压在头像上（也正好是爪背朝前的那些）

SIZE = 160                      # 输出边长
WORK = SIZE * 2                 # 合成时的工作尺寸
VIEW = (-150, 40, 880)          # 合成画布在素材坐标里的范围：左上角 x、y 和边长
AVATAR_D = 500                  # 头像直径（素材坐标，输出约 91 像素）
AVATAR_CX = 290                 # 头像中心 x：和爪子中线对齐
AVATAR_BOTTOM = 905             # 头像底边（素材坐标），形变时固定不动
FLAT = 0.4                      # 拍扁时高度比例
STRETCH = 1.1                   # 收爪回弹时的最大高度比例


def durations_cs() -> list[list[int]]:
    """与 anim.save_gif 对应的格式：每个输出帧一个时长（1/100 秒）。"""
    return [[ms // 10] for ms in FRAME_MS]


@lru_cache(maxsize=1)
def paw_frames() -> tuple[Image.Image, ...]:
    """20 张关键帧，整张缩到工作尺寸（不裁切）。"""
    k = WORK / VIEW[2]
    size = (round(SOURCE_W * k), round(SOURCE_H * k))
    return tuple(Image.open(ASSET_DIR / f"flatten_{i:02d}_source_{n:03d}.png").convert("RGBA").resize(size, Image.LANCZOS)
                 for i, n in enumerate(SOURCE_FRAMES))


def phase_ms(name: str) -> tuple[int, int]:
    """某一段的 (开始, 结束) 毫秒。"""
    t = 0
    for phase, part in PHASES:
        length = sum(ms for _, ms in part)
        if phase == name:
            return t, t + length
        t += length
    raise KeyError(name)


def _rebound(t_ms: float) -> float:
    """抬爪后 t_ms 毫秒的高度比例：从 FLAT 出发的欠阻尼弹簧，抬爪段结束时回到 100%、接着冲到 STRETCH。"""
    overshoot = (STRETCH - 1) / (1 - FLAT)
    zeta = -math.log(overshoot) / math.sqrt(math.pi ** 2 + math.log(overshoot) ** 2)
    ratio = zeta / math.sqrt(1 - zeta * zeta)
    start, end = phase_ms("release")
    wd = (math.pi - math.atan(1 / ratio)) / ((end - start) / 1000)   # 第一次回到 100% 的时刻 = 抬爪段结束
    t = t_ms / 1000
    return 1 - (1 - FLAT) * math.exp(-ratio * wd * t) * (math.cos(wd * t) + ratio * math.sin(wd * t))


def heights() -> list[float]:
    """每个输出帧（取帧中间时刻）的头像高度比例。拍到之前 1.0，爪子压着时 FLAT，抬爪后弹簧回弹，
    回到悬停那段把剩下的余振线性收到 0，保证首尾无缝。"""
    release, _ = phase_ms("release")
    settle, end = phase_ms("settle")
    out, t = [], 0
    for key, ms in zip(TIMELINE, FRAME_MS):
        mid = t + ms / 2
        if key in CONTACT:
            out.append(FLAT)
        elif mid < release:
            out.append(1.0)
        else:
            taper = 1.0 if mid < settle else (end - mid) / (end - settle)
            out.append(1 + (_rebound(mid - release) - 1) * taper)
        t += ms
    return out


def width_for(height: float) -> float:
    """压到 FLAT 时宽 120%，拉长时相应变窄。"""
    return 1 + (1 - height) * (0.2 / (1 - FLAT))


def frames(avatar: bytes) -> list[Image.Image]:
    k = WORK / VIEW[2]
    d = round(AVATAR_D * k)
    disc = anim.avatar_disc(avatar, d)
    paws = paw_frames()
    bottom = (AVATAR_BOTTOM - VIEW[1]) * k
    cx = (AVATAR_CX - VIEW[0]) * k
    paw_at = (round(-VIEW[0] * k), round(-VIEW[1] * k))
    out = []
    for key, h in zip(TIMELINE, heights()):
        canvas = Image.new("RGBA", (WORK, WORK), (0, 0, 0, 0))
        w_px, h_px = max(1, round(d * width_for(h))), max(1, round(d * h))
        body = disc if (w_px, h_px) == (d, d) else disc.resize((w_px, h_px), Image.LANCZOS)
        canvas.alpha_composite(body, (round(cx - w_px / 2), round(bottom - h_px)))
        # alpha_composite 不接受负坐标：负的部分从贴图里裁掉
        canvas.alpha_composite(paws[key], (max(0, paw_at[0]), max(0, paw_at[1])),
                               (max(0, -paw_at[0]), max(0, -paw_at[1])))
        out.append(anim.finish_frame(canvas, SIZE))
    return out


def make_gif(avatar: bytes) -> bytes:
    """返回 GIF 字节。头像无法识别时抛 PIL.UnidentifiedImageError。"""
    return anim.save_gif(frames(avatar), durations_cs())

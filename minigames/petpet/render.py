"""踩奶 GIF：用 assets/kneading/ 里提供的踩奶猫爪关键帧（Seedance 视频抽帧，透明 PNG）叠在头像上，左右爪交替踩。

猫爪不重画：
- 关键帧都是 960×470，按原坐标贴在 960×960 的虚拟画布左上角 (0, 0)；
- 每帧在两只爪子之间的透明缝（x≈468～500，所有帧都是空的）处切成左、右两层，原图像素不动；
- 左爪按原顺序播放，右爪错开半个循环（PHASE_OFFSET 帧），一只按下时另一只抬起；
- 右爪整体比左爪高 STAGGER 像素，静止位置略微错开；
- 每帧时长按原视频帧号间隔换算（24fps），再整体 SPEED 倍速播放。
头像是圆形，放在猫爪下面：哪只爪子按下，头顶对应位置就局部凹下去一点，头像也歪向那一侧 2～3°。
头像的形变用欠阻尼弹簧跟随按压深度：抬爪时会反弹过头一点（头顶微微鼓起、整体略高）再回稳。
为了让回弹看得见，每张关键帧拆成 SUBFRAMES 个子帧——猫爪图和总时长都不变，只有头像在子帧间运动；
弹簧先空跑几圈取周期稳态，所以 GIF 首尾无缝。
渲染时先把虚拟画布等比缩到 WORK 尺寸再合成，最后缩到 SIZE 输出。
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image

from . import anim

ASSET_DIR = Path(__file__).parent / "assets" / "kneading"
SOURCE_FRAMES = [0, 8, 17, 26, 35, 43, 52, 61, 70, 78, 87, 96]   # 关键帧在原视频里的帧号
SOURCE_FPS = 24
LAST_GAP = 9               # 最后一帧到下一循环第一帧的间隔（帧），沿用平均抽帧间隔
SPEED = 3                  # 整体播放倍速（猫爪节奏和头像弹簧一起加快）

VIRTUAL = 960              # 原素材的虚拟画布边长
SPLIT_X = 484              # 左右爪之间透明缝的中线（原素材坐标）
PHASE_OFFSET = len(SOURCE_FRAMES) // 2   # 右爪比左爪错开半个循环
SIZE = 160                 # 输出边长
WORK = SIZE * 2            # 合成时的工作尺寸
STAGGER = 6                # 右爪比左爪高几像素（输出像素）

AVATAR_D = 660             # 头像直径（虚拟画布坐标）
AVATAR_BOTTOM = 925        # 头像底边
DENT = 0.06                # 爪子按到底时，头顶局部凹下去的深度（占头像直径）
DENT_WIDTH = 0.22          # 凹陷的横向范围（高斯 σ，占头像直径）
TILT = 2.5                 # 按到底时头像歪向那只爪子的角度
MESH_STRIPS = 40

SUBFRAMES = 3              # 每张关键帧拆成几个子帧（猫爪不变，只给头像回弹用）
SPRING_HZ = 3.2            # 头像弹簧的固有频率
SPRING_DAMPING = 0.32      # 阻尼比 < 1：欠阻尼，抬爪时会反弹过头一点
SPRING_WARMUP = 4          # 先空跑几圈进入周期稳态，保证首尾衔接
BOUNCE = 0.035             # 整体压扁 / 拉高的幅度


def durations_ms() -> list[int]:
    """每张关键帧的时长（毫秒）：原视频帧号间隔换算，再按 SPEED 倍速。"""
    return anim.keyframe_ms(SOURCE_FRAMES, SOURCE_FPS, LAST_GAP, SPEED)


def subframe_cs() -> list[list[int]]:
    return anim.split_cs(durations_ms(), SUBFRAMES)


def spring_track(targets: list[float], cs: list[list[int]]) -> list[float]:
    """头像对按压的弹性跟随（弹簧随 SPEED 同步加快，回弹手感不变）。"""
    return anim.spring_track(targets, cs, SPRING_HZ * SPEED, SPRING_DAMPING, SPRING_WARMUP)


@lru_cache(maxsize=1)
def paw_layers() -> dict:
    """把每张关键帧切成左右两层并缩到工作尺寸。

    返回 {"left": [...], "right": [...], "press_l": (...), "press_r": (...), "cx_l": x, "cx_r": x}：
    press 是每帧该爪最低点在所有帧里的相对深度（0 抬起 ~ 1 按到底），cx 是该爪的平均中心 x（原素材坐标）。
    """
    k = WORK / VIRTUAL
    out = {"left": [], "right": []}
    bottoms = {"left": [], "right": []}
    centers = {"left": [], "right": []}
    for i, n in enumerate(SOURCE_FRAMES):
        im = Image.open(ASSET_DIR / f"paw_{i:02d}_sourceframe_{n:03d}.png").convert("RGBA")
        for side, box in (("left", (0, 0, SPLIT_X, im.height)), ("right", (SPLIT_X, 0, im.width, im.height))):
            layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
            layer.paste(im.crop(box), box[:2])              # 原坐标不变，另一半留空
            bbox = layer.getchannel("A").getbbox()
            bottoms[side].append(bbox[3])
            centers[side].append((bbox[0] + bbox[2]) / 2)
            out[side].append(layer.resize((WORK, round(im.height * k)), Image.LANCZOS))
    out["press_l"], out["press_r"] = anim.norm(bottoms["left"]), anim.norm(bottoms["right"])
    out["cx_l"] = sum(centers["left"]) / len(centers["left"])
    out["cx_r"] = sum(centers["right"]) / len(centers["right"])
    return out


def _dent(disc: Image.Image, dents: list[tuple[float, float]], pad: int) -> Image.Image:
    """头顶局部形变：dents 是 [(中心 x，深度像素)]，正值往下凹、负值（回弹）往上鼓。底边不动。

    返回的图在头像上方多留 pad 像素透明空间给往上鼓的部分。每一竖条把整列内容线性拉伸 / 压缩。
    """
    d = disc.width
    h = d + pad
    padded = Image.new("RGBA", (d, h), (0, 0, 0, 0))
    padded.paste(disc, (0, pad))
    sigma = DENT_WIDTH * d

    def src_top(x: float) -> float:
        dep = sum(dep * math.exp(-((x - cx) ** 2) / (2 * sigma ** 2)) for cx, dep in dents)
        dep = max(-pad * 0.9, min(dep, d * 0.3))
        # 目标 y=pad+dep 处取源 y=pad（头顶），目标 y=h 处取源 y=h（底边）
        return pad - (pad + dep) * d / (d - dep)

    mesh = []
    for j in range(MESH_STRIPS):
        x0, x1 = d * j / MESH_STRIPS, d * (j + 1) / MESH_STRIPS
        mesh.append(((round(x0), 0, round(x1), h),
                     (x0, src_top(x0), x0, h, x1, h, x1, src_top(x1))))
    return padded.transform((d, h), Image.MESH, mesh, Image.BICUBIC)


def frames(avatar: bytes) -> list[Image.Image]:
    """所有子帧（关键帧数 × SUBFRAMES）。同一关键帧的子帧里猫爪不变，只有头像在回弹。"""
    k = WORK / VIRTUAL
    d = round(AVATAR_D * k)
    pad = round(d * DENT * 1.5)
    disc = anim.avatar_disc(avatar, d)
    layers = paw_layers()
    n = len(SOURCE_FRAMES)
    left_x = (layers["cx_l"] - (VIRTUAL - AVATAR_D) / 2) * k     # 两只爪子在头像坐标里的 x
    right_x = (layers["cx_r"] - (VIRTUAL - AVATAR_D) / 2) * k
    stagger = round(STAGGER * WORK / SIZE)
    bottom = round(AVATAR_BOTTOM * k)
    cs = subframe_cs()
    right_idx = [(i + PHASE_OFFSET) % n for i in range(n)]
    xl = spring_track(list(layers["press_l"]), cs)
    xr = spring_track([layers["press_r"][j] for j in right_idx], cs)

    out = []
    for f, (a, b) in enumerate(zip(xl, xr)):
        i = f // SUBFRAMES
        head = _dent(disc, [(left_x, DENT * d * a), (right_x, DENT * d * b)], pad)
        # 整体也跟着弹：按下略扁，回弹时略高
        sq = BOUNCE * (a + b)
        head = head.resize((round(d * (1 + sq * 0.6)), round(head.height * (1 - sq))), Image.LANCZOS)
        head = head.rotate(TILT * (a - b), resample=Image.BICUBIC, expand=True)    # 正 = 逆时针，歪向左爪

        canvas = Image.new("RGBA", (WORK, WORK), (0, 0, 0, 0))
        center_y = bottom - (d + pad) * (1 - sq) / 2           # 未旋转时底边贴住 bottom；旋转绕中心
        canvas.alpha_composite(head, ((WORK - head.width) // 2, round(center_y - head.height / 2)))
        canvas.alpha_composite(layers["left"][i], (0, 0))
        canvas.alpha_composite(layers["right"][right_idx[i]], (0, -stagger))
        out.append(anim.finish_frame(canvas, SIZE))
    return out


def make_gif(avatar: bytes) -> bytes:
    """返回 GIF 字节。头像无法识别时抛 PIL.UnidentifiedImageError。"""
    return anim.save_gif(frames(avatar), subframe_cs())

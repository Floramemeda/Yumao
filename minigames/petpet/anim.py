"""踩奶 / 戳戳等猫爪 GIF 共用的渲染工具：圆形头像、关键帧时长、弹簧回弹、GIF 导出。纯 Pillow。"""
from __future__ import annotations

import io
import math

from PIL import Image, ImageChops, ImageDraw


def avatar_disc(raw: bytes, d: int) -> Image.Image:
    """头像裁成圆形（d×d，RGBA）。动态头像取第一帧。"""
    img = Image.open(io.BytesIO(raw))
    img.seek(0)
    img = img.convert("RGBA")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((d, d), Image.LANCZOS)
    mask = Image.new("L", (d * 4, d * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d * 4 - 1, d * 4 - 1), fill=255)
    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask.resize((d, d), Image.LANCZOS)))
    return img


def norm(values: list[float]) -> tuple[float, ...]:
    """线性归一化到 0~1。"""
    lo, hi = min(values), max(values)
    return tuple((v - lo) / (hi - lo) if hi > lo else 0.0 for v in values)


def keyframe_ms(source_frames: list[int], fps: float, last_gap: int, speed: float) -> list[int]:
    """每张关键帧的时长（毫秒）：原视频帧号间隔换算，再按 speed 倍速。"""
    nxt = source_frames[1:] + [source_frames[-1] + last_gap]
    return [round((b - a) * 1000 / fps / speed) for a, b in zip(source_frames, nxt)]


def split_cs(durations_ms: list[int], parts: int) -> list[list[int]]:
    """每张关键帧拆成 parts 个子帧的时长（GIF 的 1/100 秒单位），子帧时长相加等于原时长。"""
    out = []
    for ms in durations_ms:
        base, extra = divmod(round(ms / 10), parts)
        out.append([base + (1 if s < extra else 0) for s in range(parts)])
    return out


def spring_track(targets: list[float], cs: list[list[int]], hz: float, damping: float, warmup: int) -> list[float]:
    """弹性跟随：目标在每张关键帧内保持不变，用欠阻尼弹簧追上去，离开时会略微反弹过头。

    连续模拟 warmup 圈后取最后一圈（周期稳态），所以末帧和首帧接得上。返回每个子帧结束时的位移（跟手不拖拍）。
    """
    omega = 2 * math.pi * hz
    x = v = 0.0
    samples: list[float] = []
    for _ in range(warmup):
        samples = []
        for target, parts in zip(targets, cs):
            for part in parts:
                for _ in range(part * 10):            # 1ms 积分
                    a = omega * omega * (target - x) - 2 * damping * omega * v
                    v += a * 0.001
                    x += v * 0.001
                samples.append(x)
    return samples


def finish_frame(canvas: Image.Image, size: int) -> Image.Image:
    """缩到输出尺寸；GIF 只有全透明 / 不透明，半透明边缘先二值化，避免出现一圈杂色描边。"""
    frame = canvas.resize((size, size), Image.LANCZOS)
    frame.putalpha(frame.getchannel("A").point(lambda v: 255 if v >= 128 else 0))
    return frame


def save_gif(frames: list[Image.Image], cs: list[list[int]]) -> bytes:
    """无限循环、透明背景的 GIF。cs 是 split_cs 的结果（与 frames 一一对应）。"""
    buf = io.BytesIO()
    durations = [c * 10 for parts in cs for c in parts]
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   disposal=2, optimize=True)
    return buf.getvalue()

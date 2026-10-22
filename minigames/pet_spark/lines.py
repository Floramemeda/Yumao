"""撸猫续火花：配置驱动的文案池。

lines.json 结构：{"groups": {组名: {池名: 池}}, "fallback": 兜底文案}
池：{"enabled": bool, "weight": 数字, "min_level": 火花等级下限, "entries": [文案],
     "include_chat_responses": 可选，复用 responses.CHAT_RESPONSES 里某个关键词的回复}

文案里只替换白名单占位符（{mention} {streak} {level_name}），
用户输入永远不会进入文案，因此不会拼出意外的 mention。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

_DIR = Path(__file__).parent
DEFAULT_FALLBACK = "喵。"


def load_lines() -> dict:
    with open(_DIR / "lines.json", encoding="utf-8") as f:
        data = json.load(f)
    _resolve_includes(data)
    return data


def _resolve_includes(data: dict) -> None:
    """把 include_chat_responses 展开成条目；只读引用旧的唱歌/聊天文案，不修改它们。"""
    try:
        from responses import CHAT_RESPONSES
    except Exception:  # 独立运行或旧模块不可用时跳过
        CHAT_RESPONSES = {}
    for group in data.get("groups", {}).values():
        for pool in group.values():
            key = pool.get("include_chat_responses")
            if key:
                pool["entries"] = list(pool.get("entries", [])) + list(CHAT_RESPONSES.get(key, []))


def pick(lines: dict, group: str, level: int, rng: random.Random) -> str:
    """从某组里按权重选一个可用池，再随机选一条；没有可用文案时返回兜底。"""
    pools = [
        p for p in lines.get("groups", {}).get(group, {}).values()
        if p.get("enabled", True) and p.get("entries") and level >= p.get("min_level", 0)
        and p.get("weight", 1) > 0
    ]
    if not pools:
        return lines.get("fallback") or DEFAULT_FALLBACK
    pool = rng.choices(pools, weights=[p.get("weight", 1) for p in pools])[0]
    return rng.choice(pool["entries"])


def milestone_days(lines: dict) -> set[int]:
    """里程碑天数 = milestone 组下的池名（如 "7"、"30"）。"""
    return {int(name) for name in lines.get("groups", {}).get("milestone", {}) if name.isdigit()}


def milestone_line(lines: dict, streak: int, rng: random.Random) -> str | None:
    """连续天数恰好是里程碑时，必定从对应池里取一条；否则返回 None。不参与普通随机抽取。"""
    pool = lines.get("groups", {}).get("milestone", {}).get(str(streak))
    if not pool or not pool.get("enabled", True) or not pool.get("entries"):
        return None
    return rng.choice(pool["entries"])


def render(text: str, *, mention: str = "", streak: int = 0, level_name: str = "") -> str:
    """只替换白名单占位符；其余花括号原样保留。"""
    return (text.replace("{mention}", mention)
                .replace("{streak}", str(streak))
                .replace("{level_name}", level_name))

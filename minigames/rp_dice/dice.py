"""RP 骰点：表达式解析、掷骰与判定。纯函数，不依赖 Discord。

表达式：[关键词] <骰数>d<面数>[ +/- <加值> ][ <运算符> <阈值> ] [关键词]，运算符 > >= < <= = ==；
比较对象是点数总和加上加值。关键词可写在前面或后面：
- 优势 / adv、劣势 / dis：整组骰子掷两次，优势取对判定更有利的一次（判定是 < / <= 时取较小，其余取较大），劣势相反；
- 暗骰：结果只给掷骰人看（由 Discord 层处理）。
"""
from __future__ import annotations

import operator
import re
import secrets
from dataclasses import dataclass

KEYWORDS = {"暗骰": "hidden", "优势": "adv", "adv": "adv", "劣势": "dis", "dis": "dis"}
_KW = "|".join(KEYWORDS)
EXPR = re.compile(rf"((?:(?:{_KW})\s*)*)(\d+)\s*[dD]\s*(\d+)(?:\s*([+-])\s*(\d+))?"
                  rf"(?:\s*(>=|<=|==|>|<|=)\s*(\d+))?((?:\s*(?:{_KW}))*)", re.IGNORECASE)

OPS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "=": operator.eq,
}
MODE_LABELS = {"adv": "优势", "dis": "劣势"}


class DiceError(Exception):
    """表达式合法但超出限额等，可展示给用户。"""


@dataclass(frozen=True)
class DiceExpr:
    count: int
    sides: int
    op: str | None = None        # 已规范化："==" 统一为 "="
    threshold: int | None = None
    modifier: int = 0
    mode: str | None = None      # "adv" / "dis" / None
    hidden: bool = False

    def __str__(self) -> str:
        text = f"{self.count}d{self.sides}"
        if self.modifier:
            text += f"{self.modifier:+d}"
        if self.op:
            text += f"{self.op}{self.threshold}"
        if self.mode:
            text += f"（{MODE_LABELS[self.mode]}）"
        return text


@dataclass(frozen=True)
class RollResult:
    expr: DiceExpr
    rolls: tuple[int, ...]       # 采用的那一组
    total: int                   # 点数和 + 加值
    success: bool | None         # 无运算符时为 None
    sets: tuple[tuple[int, ...], ...] | None = None    # 优势 / 劣势时两组都记下（按掷出顺序）


def parse(text: str, limits: dict | None = None) -> DiceExpr | None:
    """整条文本（去首尾空白）恰好是骰式时返回 DiceExpr；否则 None。超限抛 DiceError。"""
    m = EXPR.fullmatch(text.strip())
    if m is None:
        return None
    count, sides = int(m.group(2)), int(m.group(3))
    modifier = int(m.group(5)) if m.group(5) is not None else 0
    if m.group(4) == "-":
        modifier = -modifier
    op = m.group(6)
    threshold = int(m.group(7)) if m.group(7) is not None else None
    if op == "==":
        op = "="
    words = [KEYWORDS[w.lower()] for w in re.findall(_KW, m.group(1) + m.group(8), re.IGNORECASE)]
    if len(words) != len(set(words)):
        raise DiceError("同一个关键词只能写一次。")
    if "adv" in words and "dis" in words:
        raise DiceError("优势和劣势不能同时用。")
    mode = next((w for w in words if w in MODE_LABELS), None)
    if limits:
        if not limits["min_count"] <= count <= limits["max_count"]:
            raise DiceError(f"骰子数量要在 {limits['min_count']}–{limits['max_count']} 之间。")
        if not limits["min_sides"] <= sides <= limits["max_sides"]:
            raise DiceError(f"骰子面数要在 {limits['min_sides']}–{limits['max_sides']} 之间。")
        if threshold is not None and not 0 <= threshold <= limits["max_threshold"]:
            raise DiceError(f"判定值要在 0–{limits['max_threshold']} 之间。")
        if abs(modifier) > limits["max_modifier"]:
            raise DiceError(f"加值 / 减值最多 {limits['max_modifier']}。")
    elif count < 1 or sides < 1:
        raise DiceError("骰子数量和面数都必须是正整数。")
    return DiceExpr(count, sides, op, threshold, modifier, mode, "hidden" in words)


def roll(expr: DiceExpr, rand=None) -> RollResult:
    """每颗骰子独立均匀取 [1, sides]；rand(n) 返回 [0, n) 的整数（默认 secrets.randbelow）。"""
    rand = rand or secrets.randbelow

    def one_set() -> tuple[int, ...]:
        return tuple(rand(expr.sides) + 1 for _ in range(expr.count))

    rolls, sets = one_set(), None
    if expr.mode:
        sets = (rolls, one_set())
        want_high = (expr.mode == "adv") != (expr.op in ("<", "<="))
        rolls = (max if want_high else min)(sets, key=sum)
    total = sum(rolls) + expr.modifier
    success = OPS[expr.op](total, expr.threshold) if expr.op else None
    return RollResult(expr, rolls, total, success, sets)


def _dice_text(rolls: tuple[int, ...]) -> str:
    return str(rolls[0]) if len(rolls) == 1 else f"[{', '.join(map(str, rolls))}]"


def format_result(r: RollResult) -> str:
    """例：🎲 1d100 → 99 | ✅ 成功（99 > 98）；🎲 2d6 → [2, 5]，合计 7；🎲 1d100-20 → 57-20 = 37；
    🎲 1d20+5（优势） → 7 / 18，取 18 → 18+5 = 23"""
    e = r.expr
    raw = sum(r.rolls)
    if r.sets is not None:
        labels = [_dice_text(s) if e.count == 1 else f"{_dice_text(s)}={sum(s)}" for s in r.sets]
        shown = f"{' / '.join(labels)}，取 {raw}"
        if e.modifier:
            shown += f" → {raw}{e.modifier:+d} = {r.total}"
    elif e.modifier:
        shown = f"{_dice_text(r.rolls)}{e.modifier:+d} = {r.total}" if e.count == 1 else \
            f"{_dice_text(r.rolls)}{e.modifier:+d}，合计 {r.total}"
    else:
        shown = str(r.total) if e.count == 1 else f"{_dice_text(r.rolls)}，合计 {r.total}"
    label = f"{e.count}d{e.sides}" + (f"{e.modifier:+d}" if e.modifier else "")
    if e.mode:
        label += f"（{MODE_LABELS[e.mode]}）"
    text = f"🎲 {label} → {shown}"
    if e.op:
        cond = f"{r.total} {e.op} {e.threshold}"
        text += f" | ✅ 成功（{cond}）" if r.success else f" | ❌ 失败（需要 {e.op} {e.threshold}）"
    return text

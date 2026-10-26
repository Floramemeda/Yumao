"""RP 骰点：表达式解析、掷骰与判定。纯函数，不依赖 Discord。

表达式：<骰数>d<面数>[ <运算符> <阈值> ]，运算符 > >= < <= = ==；比较对象是点数总和。
"""
from __future__ import annotations

import operator
import re
import secrets
from dataclasses import dataclass

EXPR = re.compile(r"(\d+)\s*[dD]\s*(\d+)(?:\s*(>=|<=|==|>|<|=)\s*(\d+))?")

OPS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "=": operator.eq,
}


class DiceError(Exception):
    """表达式合法但超出限额等，可展示给用户。"""


@dataclass(frozen=True)
class DiceExpr:
    count: int
    sides: int
    op: str | None = None        # 已规范化："==" 统一为 "="
    threshold: int | None = None

    def __str__(self) -> str:
        base = f"{self.count}d{self.sides}"
        return f"{base}{self.op}{self.threshold}" if self.op else base


@dataclass(frozen=True)
class RollResult:
    expr: DiceExpr
    rolls: tuple[int, ...]
    total: int
    success: bool | None         # 无运算符时为 None


def parse(text: str, limits: dict | None = None) -> DiceExpr | None:
    """整条文本（去首尾空白）恰好是骰式时返回 DiceExpr；否则 None。超限抛 DiceError。"""
    m = EXPR.fullmatch(text.strip())
    if m is None:
        return None
    count, sides = int(m.group(1)), int(m.group(2))
    op = m.group(3)
    threshold = int(m.group(4)) if m.group(4) is not None else None
    if op == "==":
        op = "="
    if limits:
        if not limits["min_count"] <= count <= limits["max_count"]:
            raise DiceError(f"骰子数量要在 {limits['min_count']}–{limits['max_count']} 之间。")
        if not limits["min_sides"] <= sides <= limits["max_sides"]:
            raise DiceError(f"骰子面数要在 {limits['min_sides']}–{limits['max_sides']} 之间。")
        if threshold is not None and not 0 <= threshold <= limits["max_threshold"]:
            raise DiceError(f"判定值要在 0–{limits['max_threshold']} 之间。")
    elif count < 1 or sides < 1:
        raise DiceError("骰子数量和面数都必须是正整数。")
    return DiceExpr(count, sides, op, threshold)


def roll(expr: DiceExpr, rand=None) -> RollResult:
    """每颗骰子独立均匀取 [1, sides]；rand(n) 返回 [0, n) 的整数（默认 secrets.randbelow）。"""
    rand = rand or secrets.randbelow
    rolls = tuple(rand(expr.sides) + 1 for _ in range(expr.count))
    total = sum(rolls)
    success = OPS[expr.op](total, expr.threshold) if expr.op else None
    return RollResult(expr, rolls, total, success)


def format_result(r: RollResult) -> str:
    """例：🎲 1d100 → 99 | ✅ 成功（99 > 98）；🎲 2d6 → [2, 5]，合计 7"""
    e = r.expr
    dice = f"{e.count}d{e.sides}"
    shown = str(r.total) if e.count == 1 else f"[{', '.join(map(str, r.rolls))}]，合计 {r.total}"
    text = f"🎲 {dice} → {shown}"
    if e.op:
        cond = f"{r.total} {e.op} {e.threshold}"
        text += f" | ✅ 成功（{cond}）" if r.success else f" | ❌ 失败（需要 {e.op} {e.threshold}）"
    return text

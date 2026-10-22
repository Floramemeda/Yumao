"""喵咪日事本：触发词识别、写入正文解析与检索规范化。纯函数，不依赖 Discord。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from opencc import OpenCC

TRIGGER = "记笔记"
_t2s = OpenCC("t2s")

# 正文里任何可解析的 Discord mention：用户/Bot、身份组、频道
MENTION_TOKEN = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>")
# 不用 \b：中文也算单词字符，"@everyone你好" 会漏掉；Discord 对子串本身就会解析
MASS_MENTION = re.compile(r"@(everyone|here)", re.IGNORECASE)


def to_simplified(s: str) -> str:
    return _t2s.convert(unicodedata.normalize("NFKC", s))


def search_key(s: str) -> str:
    """检索用：NFKC + 繁转简 + casefold。只用于比较，原文永不改写。"""
    return to_simplified(s).casefold()


@dataclass
class ParsedWrite:
    body: str            # 去掉两侧空白后的原始正文（可能为空）
    bad_mention: bool    # 正文含任何 mention / everyone / here


def parse_write(content: str, bot_id: int) -> ParsedWrite | None:
    """识别「记笔记 <@玉猫> 正文」。不是写入指令则返回 None（应完全忽略）。

    - 触发词必须在消息开头，只对它做 NFKC + 繁转简后比较；
    - 后面必须紧跟空白和当前 Bot 的真实 mention token（<@id> 或 <@!id>），纯文字 @ 不算。
    """
    stripped = content.lstrip()
    parts = stripped.split(maxsplit=1)
    if len(parts) < 2 or to_simplified(parts[0]) != TRIGGER:
        return None
    m = re.match(rf"<@!?{bot_id}>", parts[1])
    if m is None:
        return None
    body = parts[1][m.end():].strip()
    bad = bool(MENTION_TOKEN.search(body) or MASS_MENTION.search(body))
    return ParsedWrite(body=body, bad_mention=bad)

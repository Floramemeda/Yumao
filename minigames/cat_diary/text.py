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


# ---------------------------------------------------------------- 去掉 Markdown（右键「记笔记」用）

_CODE_BLOCK = re.compile(r"```(?:[\w+-]*\n)?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MASKED_LINK = re.compile(r"\[([^\]\n]+)\]\(<?(?:https?://[^)\s>]+)>?\)")
_ANGLE_LINK = re.compile(r"<(https?://[^>\s]+)>")
_CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")
_LINE_PREFIX = re.compile(r"^[ \t]*(?:#{1,3}[ \t]+|-#[ \t]+|>>>[ \t]?|>[ \t]?|[-*+][ \t]+)")
_PAIRS = [
    re.compile(r"\|\|(.+?)\|\|", re.S),
    re.compile(r"\*\*(.+?)\*\*", re.S),
    re.compile(r"~~(.+?)~~", re.S),
    re.compile(r"(?<!\w)__(.+?)__(?!\w)", re.S),
    re.compile(r"\*(?!\s)(.+?)(?<!\s)\*", re.S),
    re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", re.S),
]
_ESCAPED = re.compile(r"\\([!-/:-@\[-`{-~])")     # 反斜杠 + 任意 ASCII 标点


def strip_markdown(s: str) -> str:
    """去掉 Discord Markdown 标记，只留文字本身：粗斜体、下划线、删除线、剧透、代码、标题、引用、列表符号、
    遮罩链接（留链接文字）、自定义表情（留 :名字:）。用户手打的反斜杠转义还原成字符本身。"""
    text = s or ""
    escaped: list[str] = []

    def hold(m):                       # 先把 \* 这类转义藏起来，避免被当成标记
        escaped.append(m.group(1))
        return f"\x00{len(escaped) - 1}\x00"

    text = _ESCAPED.sub(hold, text)
    text = _CODE_BLOCK.sub(lambda m: m.group(1).strip("\n"), text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _MASKED_LINK.sub(r"\1", text)
    text = _ANGLE_LINK.sub(r"\1", text)
    text = _CUSTOM_EMOJI.sub(r":\1:", text)
    text = "\n".join(_LINE_PREFIX.sub("", line) for line in text.split("\n"))
    for _ in range(3):                 # 嵌套标记（***粗斜体***、**__下划线粗体__**）多剥几层
        before = text
        for pat in _PAIRS:
            text = pat.sub(r"\1", text)
        if text == before:
            break
    text = re.sub(r"\x00(\d+)\x00", lambda m: escaped[int(m.group(1))], text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

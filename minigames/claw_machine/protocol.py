"""门票 Bot → 玉猫 的跨 Bot 启动协议（V1）。

门票 Bot（造化伞娘）现在发的格式：
    <@玩家ID> 使用了一张好运无限抽奖券！
    ...
    -# ||[YUNZHOU_CLAW_START]||
    -# ||EVENT_ID=7f3a9c2e41b8||
每行前面的 `-#`（小字）和外面的 `||`（剧透）、反引号都会先去掉再解析。
- 玩家：优先取标记后的 USER_ID=…；没有就取消息里第一个 <@玩家ID>。
- 版本：VERSION 可省略（视为 1）；写了但不是 1 一律忽略。
- EVENT_ID 必填。
标记可以在正文，也可以在 Embed 描述里（两处都找）。是否来自可信 Bot、可信频道由 cog 判断。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MARKER = "[YUNZHOU_CLAW_START]"
VERSION = "1"
_KV = re.compile(r"^([A-Z_]+)\s*=\s*(\S+)$")
_USER = re.compile(r"^\d{15,21}$")
_EVENT = re.compile(r"^[A-Za-z0-9_.:\-]{1,100}$")
_MENTION = re.compile(r"<@!?(\d{15,21})>")


@dataclass(frozen=True)
class StartTicket:
    user_id: int
    event_id: str


def _texts(message) -> list[str]:
    out = [message.content or ""]
    for e in getattr(message, "embeds", None) or []:
        out.append(getattr(e, "description", None) or "")
    return out


def _clean(line: str) -> str:
    """去掉 Discord 的小字前缀 -#、剧透 || 和反引号。"""
    s = line.strip()
    if s.startswith("-#"):
        s = s[2:].strip()
    return s.strip("|`").strip()


def parse_text(text: str) -> StartTicket | None:
    """标记之后的 KEY=VALUE 行。版本不对、字段缺失或格式不对一律返回 None。"""
    i = text.find(MARKER)
    if i < 0:
        return None
    fields: dict[str, str] = {}
    for line in text[i + len(MARKER):].splitlines():
        m = _KV.match(_clean(line))
        if m and m[1] not in fields:
            fields[m[1]] = m[2]
    if fields.get("VERSION", VERSION) != VERSION:
        return None
    user = fields.get("USER_ID")
    if user is None:
        mention = _MENTION.search(text[:i])
        user = mention[1] if mention else ""
    event = fields.get("EVENT_ID", "")
    if not _USER.match(user) or not _EVENT.match(event):
        return None
    return StartTicket(int(user), event)


def parse_message(message) -> StartTicket | None:
    for text in _texts(message):
        ticket = parse_text(text)
        if ticket is not None:
            return ticket
    return None

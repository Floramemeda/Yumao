"""门票 Bot → 玉猫 的跨 Bot 启动协议（V1）。

门票 Bot 发的消息里含一段：
    [YUNZHOU_CLAW_START]
    VERSION=1
    USER_ID=<Discord 用户 ID>
    EVENT_ID=<唯一事件 ID>
标记可以在正文，也可以在 Embed 描述里（两处都找）。是否来自可信 Bot、可信频道由 cog 判断。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MARKER = "[YUNZHOU_CLAW_START]"
VERSION = "1"
_KV = re.compile(r"^\s*([A-Z_]+)\s*=\s*(\S+)\s*$")
_USER = re.compile(r"^\d{15,21}$")
_EVENT = re.compile(r"^[A-Za-z0-9_.:\-]{1,100}$")


@dataclass(frozen=True)
class StartTicket:
    user_id: int
    event_id: str


def _texts(message) -> list[str]:
    out = [message.content or ""]
    for e in getattr(message, "embeds", None) or []:
        out.append(getattr(e, "description", None) or "")
    return out


def parse_text(text: str) -> StartTicket | None:
    """标记之后的 KEY=VALUE 行。版本不对、字段缺失或格式不对一律返回 None。"""
    i = text.find(MARKER)
    if i < 0:
        return None
    fields: dict[str, str] = {}
    for line in text[i + len(MARKER):].splitlines():
        m = _KV.match(line.strip("`"))
        if m and m[1] not in fields:
            fields[m[1]] = m[2]
    if fields.get("VERSION") != VERSION:
        return None
    user, event = fields.get("USER_ID", ""), fields.get("EVENT_ID", "")
    if not _USER.match(user) or not _EVENT.match(event):
        return None
    return StartTicket(int(user), event)


def parse_message(message) -> StartTicket | None:
    for text in _texts(message):
        ticket = parse_text(text)
        if ticket is not None:
            return ticket
    return None

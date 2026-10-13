"""小游戏共用工具：频道白名单、JSON 存档。"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def channel_allowed(channel, allowed_ids) -> bool:
    """频道本身或其所在子区的父频道在白名单中即可。"""
    if channel is None:
        return False
    return channel.id in allowed_ids or getattr(channel, "parent_id", None) in allowed_ids


def thread_under(channel, parent_ids) -> bool:
    """只允许指定频道下的子区（频道本身不行）。"""
    return getattr(channel, "parent_id", None) in parent_ids


def is_admin(interaction) -> bool:
    """服务器管理员：Administrator、管理服务器或管理消息权限。"""
    perms = getattr(interaction, "permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild or perms.manage_messages))


def write_json_atomic(path: Path, data) -> None:
    """先写临时文件再替换，避免写到一半崩溃留下损坏的存档。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

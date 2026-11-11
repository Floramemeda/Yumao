"""沙滩排球：按钮。custom_id 只编码局号与阶段号；谁能操作完全由服务端按点击者身份判断。

按钮在公开消息上，但别人看不到谁点了哪个；接球的部位按钮只出现在点击者自己看得到的消息里。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .models import DIRECTION_LABELS, DIRECTIONS, PART_LABELS, PARTS, PHASE_SERVE, VolleyballSession

if TYPE_CHECKING:
    from .service import VolleyballService

COG_NAME = "BeachVolleyball"

LOBBY_BUTTONS = {
    "join": ("加入", "🏐", discord.ButtonStyle.success),
    "leave": ("退出", "🚪", discord.ButtonStyle.secondary),
    "start": ("开始", "🔔", discord.ButtonStyle.primary),
    "cancel": ("取消", "✖️", discord.ButtonStyle.danger),
}
PART_STYLES = {
    "hand": discord.ButtonStyle.success,
    "chest": discord.ButtonStyle.danger,
    "butt": discord.ButtonStyle.danger,
}


def _service(interaction: discord.Interaction) -> VolleyballService | None:
    cog = interaction.client.get_cog(COG_NAME)
    return getattr(cog, "service", None)


async def _unavailable(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("小游戏暂时不可用。", ephemeral=True)


class LobbyButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"bv:lobby:(?P<op>join|leave|start|cancel):(?P<sid>[0-9a-f]+)"):
    def __init__(self, op: str, sid: str, disabled: bool = False):
        label, emoji, style = LOBBY_BUTTONS[op]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled,
                                           custom_id=f"bv:lobby:{op}:{sid}"))
        self.op, self.sid = op, sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["op"], match["sid"])

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            return await _unavailable(interaction)
        await service.handle_lobby(interaction, self.sid, self.op)


class ServeButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"bv:serve:(?P<direction>left|right):(?P<sid>[0-9a-f]+):(?P<seq>\d+)"):
    def __init__(self, direction: str, sid: str, seq: int, disabled: bool = False):
        super().__init__(discord.ui.Button(label=f"发向{DIRECTION_LABELS[direction]}", emoji="🏐",
                                           style=discord.ButtonStyle.primary, disabled=disabled,
                                           custom_id=f"bv:serve:{direction}:{sid}:{seq}"))
        self.direction, self.sid, self.seq = direction, sid, seq

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["direction"], match["sid"], int(match["seq"]))

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            return await _unavailable(interaction)
        await service.handle_serve(interaction, self.sid, self.seq, self.direction)


class AimButton(discord.ui.DynamicItem[discord.ui.Button],
                template=r"bv:aim:(?P<direction>left|right):(?P<sid>[0-9a-f]+):(?P<seq>\d+)"):
    """接球第一步（公开面板）：选方向，弹出只有自己看得到的部位按钮。"""

    def __init__(self, direction: str, sid: str, seq: int, disabled: bool = False):
        super().__init__(discord.ui.Button(label=f"接{DIRECTION_LABELS[direction]}", emoji="🙌",
                                           style=discord.ButtonStyle.primary, disabled=disabled,
                                           custom_id=f"bv:aim:{direction}:{sid}:{seq}"))
        self.direction, self.sid, self.seq = direction, sid, seq

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["direction"], match["sid"], int(match["seq"]))

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            return await _unavailable(interaction)
        await service.handle_aim(interaction, self.sid, self.seq, self.direction)


class ReceiveButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"bv:recv:(?P<part>hand|chest|butt):(?P<direction>left|right)"
                             r":(?P<sid>[0-9a-f]+):(?P<seq>\d+)"):
    def __init__(self, part: str, direction: str, sid: str, seq: int, disabled: bool = False):
        super().__init__(discord.ui.Button(label=PART_LABELS[part], style=PART_STYLES[part], disabled=disabled,
                                           custom_id=f"bv:recv:{part}:{direction}:{sid}:{seq}"))
        self.part, self.direction, self.sid, self.seq = part, direction, sid, seq

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["part"], match["direction"], match["sid"], int(match["seq"]))

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            return await _unavailable(interaction)
        await service.handle_receive(interaction, self.sid, self.seq, self.part, self.direction)


class CancelButton(discord.ui.DynamicItem[discord.ui.Button], template=r"bv:cancel:(?P<sid>[0-9a-f]+)"):
    def __init__(self, sid: str, disabled: bool = False):
        super().__init__(discord.ui.Button(label="取消本局", emoji="✖️", style=discord.ButtonStyle.secondary,
                                           disabled=disabled, row=1, custom_id=f"bv:cancel:{sid}"))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"])

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            return await _unavailable(interaction)
        await service.handle_cancel(interaction, self.sid)


def lobby_view(s: VolleyballSession, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for op in LOBBY_BUTTONS:
        view.add_item(LobbyButton(op, s.id, disabled=disabled))
    return view


def game_view(s: VolleyballSession, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if s.phase == PHASE_SERVE:
        for direction in DIRECTIONS:
            view.add_item(ServeButton(direction, s.id, s.seq, disabled=disabled))
    else:
        for direction in DIRECTIONS:
            view.add_item(AimButton(direction, s.id, s.seq, disabled=disabled))
    view.add_item(CancelButton(s.id, disabled=disabled))
    return view


def part_view(s: VolleyballSession, direction: str) -> discord.ui.View:
    """接球第二步（仅自己可见）：选部位。"""
    view = discord.ui.View(timeout=None)
    for part in PARTS:
        view.add_item(ReceiveButton(part, direction, s.id, s.seq))
    return view

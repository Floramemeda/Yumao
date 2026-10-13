"""超级滚雪球：按钮。custom_id 只编码局号与棒次；谁能操作完全由服务端按点击者身份判断。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .models import ACTION_EMOJI, ACTION_LABELS, ACTIONS, SnowballSession

if TYPE_CHECKING:
    from .service import SnowballService

COG_NAME = "SnowballRelay"

LOBBY_BUTTONS = {
    "join": ("加入", "⛄", discord.ButtonStyle.success),
    "leave": ("退出", "🚪", discord.ButtonStyle.secondary),
    "start": ("开始", "🔔", discord.ButtonStyle.primary),
    "cancel": ("取消", "✖️", discord.ButtonStyle.danger),
}
ACTION_STYLES = {
    "steady": discord.ButtonStyle.success,
    "hard": discord.ButtonStyle.danger,
    "aim": discord.ButtonStyle.primary,
    "repair": discord.ButtonStyle.secondary,
}


def _service(interaction: discord.Interaction) -> SnowballService | None:
    cog = interaction.client.get_cog(COG_NAME)
    return getattr(cog, "service", None)


async def _unavailable(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("小游戏暂时不可用。", ephemeral=True)


class LobbyButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"sb:lobby:(?P<op>join|leave|start|cancel):(?P<sid>[0-9a-f]+)"):
    def __init__(self, op: str, sid: str, disabled: bool = False):
        label, emoji, style = LOBBY_BUTTONS[op]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled,
                                           custom_id=f"sb:lobby:{op}:{sid}"))
        self.op, self.sid = op, sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["op"], match["sid"])

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            return await _unavailable(interaction)
        await service.handle_lobby(interaction, self.sid, self.op)


class ActionButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"sb:act:(?P<action>steady|hard|aim|repair):(?P<sid>[0-9a-f]+):(?P<seq>\d+)"):
    def __init__(self, action: str, sid: str, seq: int, disabled: bool = False):
        super().__init__(discord.ui.Button(label=ACTION_LABELS[action], emoji=ACTION_EMOJI[action],
                                           style=ACTION_STYLES[action], disabled=disabled,
                                           custom_id=f"sb:act:{action}:{sid}:{seq}"))
        self.action, self.sid, self.seq = action, sid, seq

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["sid"], int(match["seq"]))

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            return await _unavailable(interaction)
        await service.handle_action(interaction, self.sid, self.seq, self.action)


class CancelButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sb:cancel:(?P<sid>[0-9a-f]+)"):
    def __init__(self, sid: str, disabled: bool = False):
        super().__init__(discord.ui.Button(label="取消本局", emoji="✖️", style=discord.ButtonStyle.secondary,
                                           disabled=disabled, custom_id=f"sb:cancel:{sid}"))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"])

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            return await _unavailable(interaction)
        await service.handle_cancel(interaction, self.sid)


def lobby_view(s: SnowballSession, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for op in LOBBY_BUTTONS:
        view.add_item(LobbyButton(op, s.id, disabled=disabled))
    return view


def game_view(s: SnowballSession, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ACTIONS:
        view.add_item(ActionButton(action, s.id, s.action_sequence, disabled=disabled))
    view.add_item(CancelButton(s.id, disabled=disabled))
    return view

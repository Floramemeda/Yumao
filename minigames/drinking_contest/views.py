"""千杯不醉：Discord 按钮。

使用 DynamicItem，custom_id 中编码游戏 ID 与轮次，Bot 重启后按钮仍然可用，
且旧回合按钮能被识别并拒绝。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .models import Action, DrinkingGame

if TYPE_CHECKING:
    from .service import DrinkingService

COG_NAME = "DrinkingContest"

LOBBY_BUTTONS = {
    "join": ("加入酒局", "🍶", discord.ButtonStyle.success),
    "leave": ("退出酒局", "🚪", discord.ButtonStyle.secondary),
    "start": ("开始比赛", "🔔", discord.ButtonStyle.primary),
}

ACTION_BUTTONS = {
    Action.DRINK: ("再来一杯", "🍺", discord.ButtonStyle.primary),
    Action.PRETEND: ("假装喝了", "😏", discord.ButtonStyle.secondary),
    Action.WITHDRAW: ("放弃比赛", "🏳️", discord.ButtonStyle.danger),
}


def _service(interaction: discord.Interaction) -> DrinkingService | None:
    cog = interaction.client.get_cog(COG_NAME)
    return getattr(cog, "service", None)


class LobbyButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"dc:lobby:(?P<op>join|leave|start):(?P<gid>[0-9a-f]+)"):
    def __init__(self, op: str, game_id: str, disabled: bool = False):
        label, emoji, style = LOBBY_BUTTONS[op]
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=style, disabled=disabled,
            custom_id=f"dc:lobby:{op}:{game_id}",
        ))
        self.op = op
        self.game_id = game_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["op"], match["gid"])

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            await interaction.response.send_message("小游戏暂时不可用。", ephemeral=True)
            return
        await service.handle_lobby(interaction, self.game_id, self.op)


class ActionButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"dc:act:(?P<op>drink|pretend|withdraw):(?P<gid>[0-9a-f]+):(?P<round>\d+)"):
    def __init__(self, action: Action, game_id: str, round_number: int, disabled: bool = False):
        label, emoji, style = ACTION_BUTTONS[action]
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=style, disabled=disabled,
            custom_id=f"dc:act:{action.value}:{game_id}:{round_number}",
        ))
        self.action = action
        self.game_id = game_id
        self.round_number = round_number

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(Action(match["op"]), match["gid"], int(match["round"]))

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            await interaction.response.send_message("小游戏暂时不可用。", ephemeral=True)
            return
        await service.handle_action(interaction, self.game_id, self.round_number, self.action)


def lobby_view(game: DrinkingGame, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for op in LOBBY_BUTTONS:
        view.add_item(LobbyButton(op, game.game_id, disabled=disabled))
    return view


def round_view(game: DrinkingGame, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ACTION_BUTTONS:
        view.add_item(ActionButton(action, game.game_id, game.round_number, disabled=disabled))
    return view

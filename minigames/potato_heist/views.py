"""媚笙阁偷薯饼：Discord 按钮与选择菜单。

全部使用 DynamicItem：custom_id 只编码 session_id（和轮次），不编码角色/位置等秘密，
重启后仍可用；真正的校验全部由服务端根据点击者身份完成。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .models import HeistGame, describe

if TYPE_CHECKING:
    from .service import HeistService

COG_NAME = "PotatoHeist"

LOBBY_BUTTONS = {
    "join": ("加入", "🐭", discord.ButtonStyle.success),
    "leave": ("退出", "🚪", discord.ButtonStyle.secondary),
    "start": ("开始", "🔔", discord.ButtonStyle.primary),
    "rules": ("规则", "📜", discord.ButtonStyle.secondary),
    "cancel": ("取消", "✖️", discord.ButtonStyle.danger),
}

GAME_BUTTONS = {
    "me": ("我的行动", "🕵️", discord.ButtonStyle.primary),
    "rules": ("规则", "📜", discord.ButtonStyle.secondary),
    "cancel": ("取消游戏", "✖️", discord.ButtonStyle.danger),
}


def _service(interaction: discord.Interaction) -> HeistService | None:
    cog = interaction.client.get_cog(COG_NAME)
    return getattr(cog, "service", None)


async def _unavailable(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("小游戏暂时不可用。", ephemeral=True)


class LobbyButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"ph:lobby:(?P<op>join|leave|start|rules|cancel):(?P<sid>[0-9a-f]+)"):
    def __init__(self, op: str, session_id: str, disabled: bool = False):
        label, emoji, style = LOBBY_BUTTONS[op]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled,
                                           custom_id=f"ph:lobby:{op}:{session_id}"))
        self.op, self.session_id = op, session_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["op"], match["sid"])

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            return await _unavailable(interaction)
        await service.handle_lobby(interaction, self.session_id, self.op)


class GameButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"ph:game:(?P<op>me|rules|cancel):(?P<sid>[0-9a-f]+)"):
    def __init__(self, op: str, session_id: str, disabled: bool = False):
        label, emoji, style = GAME_BUTTONS[op]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled,
                                           custom_id=f"ph:game:{op}:{session_id}"))
        self.op, self.session_id = op, session_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["op"], match["sid"])

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            return await _unavailable(interaction)
        await service.handle_game_button(interaction, self.session_id, self.op)


class ActionSelect(discord.ui.DynamicItem[discord.ui.Select],
                   template=r"ph:act:(?P<sid>[0-9a-f]+):(?P<round>\d+)"):
    """私密行动菜单，只出现在 ephemeral 消息里。选项来自服务端的 legal_actions。"""

    def __init__(self, session_id: str, round_no: int, actions: list[str] | None = None,
                 current: str | None = None):
        options = [discord.SelectOption(label=describe(a), value=a, default=(a == current))
                   for a in (actions or [])]
        super().__init__(discord.ui.Select(
            custom_id=f"ph:act:{session_id}:{round_no}",
            placeholder="选择本轮行动……",
            options=options or [discord.SelectOption(label="无", value="none")],
            disabled=not options,
        ))
        self.session_id, self.round_no = session_id, round_no

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"], int(match["round"]))

    async def callback(self, interaction: discord.Interaction):
        service = _service(interaction)
        if service is None:
            return await _unavailable(interaction)
        values = (interaction.data or {}).get("values") or []
        await service.handle_select(interaction, self.session_id, self.round_no, values[0] if values else "")


def lobby_view(game: HeistGame, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for op in LOBBY_BUTTONS:
        view.add_item(LobbyButton(op, game.session_id, disabled=disabled))
    return view


def game_view(game: HeistGame, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for op in GAME_BUTTONS:
        # 规则在结束后仍可查看
        view.add_item(GameButton(op, game.session_id, disabled=disabled and op != "rules"))
    return view


def private_view(game: HeistGame, actions: list[str], current: str | None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ActionSelect(game.session_id, game.round_no, actions, current))
    return view

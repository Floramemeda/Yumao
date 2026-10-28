"""不给糖就捣蛋：持久按钮与私密部位选择。

custom_id 只编码活动轮次（如 e3），不编码任何秘密；谁能做什么完全由服务端按点击者身份和活动当前状态判断。
全部用 DynamicItem，Bot 重启后旧面板和未提交的选择菜单仍然有效。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .constants import BODY_PARTS

if TYPE_CHECKING:
    from .cog import TrickOrTreat

COG_NAME = "TrickOrTreat"

PANEL_BUTTONS = {
    "join": ("报名藏糖", "🍬", discord.ButtonStyle.success),
    "status": ("我的状态", "📋", discord.ButtonStyle.secondary),
    "board": ("公共进度", "🔎", discord.ButtonStyle.primary),
}


def _cog(interaction: discord.Interaction) -> TrickOrTreat | None:
    return interaction.client.get_cog(COG_NAME)


async def _unavailable(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("小游戏暂时不可用。", ephemeral=True)


class PanelButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"tot:(?P<op>join|status|board):(?P<eid>e\d+)"):
    def __init__(self, op: str, eid: str, disabled: bool = False):
        text, emoji, style = PANEL_BUTTONS[op]
        super().__init__(discord.ui.Button(label=text, emoji=emoji, style=style, disabled=disabled,
                                           custom_id=f"tot:{op}:{eid}"))
        self.op, self.eid = op, eid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["op"], match["eid"])

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            return await _unavailable(interaction)
        await cog.handle_panel(interaction, self.op, self.eid)


class HideSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"tot:hide:(?P<eid>e\d+)"):
    """私密的 7 选 1 藏糖菜单；选中后才真正报名（只打开不提交不算报名）。"""

    def __init__(self, eid: str):
        super().__init__(discord.ui.Select(
            custom_id=f"tot:hide:{eid}", placeholder="把糖藏在哪里？（选定后不能修改）",
            options=[discord.SelectOption(label=text, value=pid) for pid, text in BODY_PARTS]))
        self.eid = eid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["eid"])

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            return await _unavailable(interaction)
        values = (interaction.data or {}).get("values") or []
        await cog.handle_hide(interaction, self.eid, values[0] if values else "")


def panel_view(eid: str, closed: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for op in PANEL_BUTTONS:
        view.add_item(PanelButton(op, eid, disabled=closed and op == "join"))   # 结束后仍可查看状态和进度
    return view


def hide_view(eid: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(HideSelect(eid))
    return view


class ConfirmView(discord.ui.View):
    def __init__(self, on_yes, yes_label: str):
        super().__init__(timeout=120)
        yes = discord.ui.Button(label=yes_label, style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="算了", style=discord.ButtonStyle.secondary)
        yes.callback = on_yes
        no.callback = self._no
        self.add_item(yes)
        self.add_item(no)

    async def _no(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="已取消。", view=None)


DYNAMIC_ITEMS = (PanelButton, HideSelect)

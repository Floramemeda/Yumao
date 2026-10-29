"""不给糖就捣蛋：持久按钮与私密部位选择。

custom_id 只编码活动轮次（如 e3），不编码任何秘密；谁能做什么完全由服务端按点击者身份和活动当前状态判断。
面板按钮用 DynamicItem，Bot 重启后旧面板仍然有效；报名表 Modal 的提交由 cog 按 custom_id 统一接收。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .constants import BODY_PARTS, NICKNAME_MAX_LENGTH

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


class JoinModal(discord.ui.Modal):
    """报名表：藏糖部位（必选）+ 万圣节昵称（可选）。

    提交由 TrickOrTreat.on_interaction 按 custom_id（tot:join:<轮次>）处理，Bot 重启后提交也能接住；
    只打开不提交不算报名。
    """

    def __init__(self, eid: str):
        super().__init__(title="🎃 报名藏糖", timeout=None, custom_id=f"tot:join:{eid}")
        self.add_item(discord.ui.Label(
            text="把糖藏在哪里？", description="只有你自己知道；提交后不能修改",
            component=discord.ui.Select(custom_id="part", placeholder="选择藏糖部位……",
                                        options=[discord.SelectOption(label=t, value=pid) for pid, t in BODY_PARTS])))
        self.add_item(discord.ui.Label(
            text="万圣节昵称（可选）", description="用皮套或自定义角色参加就填角色名；播报会显示昵称，不会 @ 你",
            component=discord.ui.TextInput(custom_id="nickname", required=False,
                                           max_length=NICKNAME_MAX_LENGTH, placeholder="例：南瓜灯小猫")))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        return None


def panel_view(eid: str, closed: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for op in PANEL_BUTTONS:
        view.add_item(PanelButton(op, eid, disabled=closed and op == "join"))   # 结束后仍可查看状态和进度
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


DYNAMIC_ITEMS = (PanelButton,)

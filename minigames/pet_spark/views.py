"""撸猫续火花：按钮。全部以点击者本人身份处理，custom_id 不含用户信息，重启后仍可用。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .cog import PetSpark

COG_NAME = "PetSpark"


def _cog(interaction: discord.Interaction) -> PetSpark | None:
    return interaction.client.get_cog(COG_NAME)


class PetButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pet:again"):
    """常驻「撸撸玉猫」按钮：谁点就给谁签到，与 /撸猫 等价。"""

    def __init__(self):
        super().__init__(discord.ui.Button(label="撸撸玉猫", emoji="🐾", style=discord.ButtonStyle.primary,
                                           custom_id="pet:again"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("玉猫暂时不在。", ephemeral=True)
            return
        await cog.do_pet(interaction)


class ReminderToggle(discord.ui.DynamicItem[discord.ui.Button], template=r"pet:remind:(?P<op>on|off)"):
    def __init__(self, op: str):
        label, emoji = ("开启催撸", "🔔") if op == "on" else ("关闭催撸", "🔕")
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary,
                                           custom_id=f"pet:remind:{op}"))
        self.op = op

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["op"])

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("玉猫暂时不在。", ephemeral=True)
            return
        await cog.toggle_reminder(interaction, opt_out=(self.op == "off"))


def pet_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(PetButton())
    return view


def status_view(reminders_on: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReminderToggle("off" if reminders_on else "on"))
    view.add_item(PetButton())
    return view

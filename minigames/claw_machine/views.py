"""万象娃娃机：按钮与菜单。

游戏局、结算结果、赠礼邀请上的按钮都是 DynamicItem，custom_id 只编码局号 / 候选序号 / 抓法 / 赠礼号这类公开信息，
Bot 重启后旧按钮照样能用；谁能点、点了算不算数，全部由服务端按点击者 ID 和存档状态判断。
收藏柜是仅本人可见的临时界面（普通 View），过期了重新 /claw collection 即可，不影响任何数据。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .renderer import FILTER_LABELS, NUMS

if TYPE_CHECKING:
    from .cog import ClawMachine

COG_NAME = "ClawMachine"


def _cog(interaction: discord.Interaction) -> ClawMachine | None:
    return interaction.client.get_cog(COG_NAME)


async def _unavailable(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("娃娃机暂时不可用。", ephemeral=True)


# ---------------------------------------------------------------------- 游戏局

class GameButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"claw:(?P<op>pick|tech|go|back):(?P<g>\d+)(?::(?P<i>\d))?(?::(?P<t>[a-z]+))?"):
    def __init__(self, op: str, game_id: str, idx: int | None = None, tech: str | None = None, *,
                 label: str = "", emoji: str | None = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary, row: int | None = None):
        cid = f"claw:{op}:{game_id}" + (f":{idx}" if idx is not None else "") + (f":{tech}" if tech else "")
        super().__init__(discord.ui.Button(label=label or op, emoji=emoji, style=style, custom_id=cid, row=row))
        self.op, self.game_id, self.idx, self.tech = op, game_id, idx, tech

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        idx = int(match["i"]) if match["i"] is not None else None
        return cls(match["op"], match["g"], idx, match["t"], label=item.label or "", style=item.style)

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            return await _unavailable(interaction)
        await cog.handle_game(interaction, self.op, self.game_id, self.idx, self.tech)


def pick_view(game: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for i in range(len(game["candidates"])):
        view.add_item(GameButton("pick", game["id"], i, label=f"选 {i + 1} 号", emoji=NUMS[i],
                                 style=discord.ButtonStyle.primary))
    return view


def aim_view(cfg: dict, game: dict, idx: int, tech: str | None = None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if tech is None:
        for key, t in cfg["techniques"].items():
            view.add_item(GameButton("tech", game["id"], idx, key, label=t["name"], emoji=t["emoji"],
                                     style=discord.ButtonStyle.primary))
        view.add_item(GameButton("back", game["id"], label="换一只", emoji="↩️", row=1))
    else:
        view.add_item(GameButton("go", game["id"], idx, tech, label="确认抓取", emoji="🕹️",
                                 style=discord.ButtonStyle.success))
        view.add_item(GameButton("back", game["id"], idx, label="换个抓法", emoji="↩️"))
    return view


# ---------------------------------------------------------------------- 结算后

class CollectionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"claw:col"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="查看收藏", emoji="🧸", style=discord.ButtonStyle.secondary,
                                           custom_id="claw:col"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            return await _unavailable(interaction)
        await cog.open_collection(interaction)


class GiveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"claw:give:(?P<d>[A-Za-z0-9\-]+)"):
    def __init__(self, doll_id: str):
        super().__init__(discord.ui.Button(label="赠送好友", emoji="🎁", style=discord.ButtonStyle.secondary,
                                           custom_id=f"claw:give:{doll_id}"))
        self.doll_id = doll_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["d"])

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            return await _unavailable(interaction)
        await cog.open_gift_picker(interaction, self.doll_id)


class DollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"claw:(?P<op>fav|rec):(?P<d>[A-Za-z0-9\-]+)"):
    """结算结果上的【收藏】【回收】。按钮只编码娃娃编号，点下去按存档里的当前状态切换 / 回收。"""

    def __init__(self, op: str, doll_id: str, *, label: str = "", emoji: str | None = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(label=label or op, emoji=emoji, style=style,
                                           custom_id=f"claw:{op}:{doll_id}"))
        self.op, self.doll_id = op, doll_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["op"], match["d"], label=item.label or "", style=item.style)

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            return await _unavailable(interaction)
        await cog.handle_doll_button(interaction, self.op, self.doll_id)


def result_view(doll: dict | None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if doll is not None:
        fav = doll.get("is_favorite")
        view.add_item(DollButton("fav", doll["id"], label="取消收藏" if fav else "收藏", emoji="⭐",
                                 style=discord.ButtonStyle.secondary if fav else discord.ButtonStyle.primary))
    view.add_item(CollectionButton())
    if doll is not None:
        view.add_item(GiveButton(doll["id"]))
        view.add_item(DollButton("rec", doll["id"], label="已回收" if doll.get("is_recycled") else "回收",
                                 emoji="🗑️", style=discord.ButtonStyle.danger))
    return view


# ---------------------------------------------------------------------- 赠礼邀请

GIFT_OPS = {"accept": ("收下", "💝", discord.ButtonStyle.success),
            "decline": ("婉拒", "🙅", discord.ButtonStyle.secondary),
            "cancel": ("撤回（赠送人）", "↩️", discord.ButtonStyle.danger)}


class GiftButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"claw:gift:(?P<id>\d+):(?P<op>accept|decline|cancel)"):
    def __init__(self, gift_id: str, op: str):
        label, emoji, style = GIFT_OPS[op]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style,
                                           custom_id=f"claw:gift:{gift_id}:{op}"))
        self.gift_id, self.op = gift_id, op

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["id"], match["op"])

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            return await _unavailable(interaction)
        await cog.handle_gift(interaction, self.gift_id, self.op)


def gift_view(gift_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for op in GIFT_OPS:
        view.add_item(GiftButton(gift_id, op))
    return view


# ---------------------------------------------------------------------- 收藏柜（仅本人可见）

class CollectionView(discord.ui.View):
    """收藏柜列表；flt == "recycled" 时就是回收站列表（没有筛选，底部按钮换成「返回收藏柜」）。"""

    def __init__(self, cog: ClawMachine, dolls: list[dict], flt: str, page: int, pages: int):
        super().__init__(timeout=600)
        self.cog, self.flt, self.page = cog, flt, page
        in_bin = flt == "recycled"
        if dolls:
            pick = discord.ui.Select(placeholder="选一只查看 / 恢复……" if in_bin else "查看详情……", row=0, options=[
                discord.SelectOption(label=d["name"][:100], value=d["id"], description=d["id"],
                                     emoji="⭐" if d.get("is_favorite") else "✨" if d["egg"] else "🧸")
                for d in dolls])
            pick.callback = self._pick
            self.add_item(pick)
        if not in_bin:
            flt_select = discord.ui.Select(placeholder="筛选", row=1, options=[
                discord.SelectOption(label=f"筛选：{text}", value=key, default=key == flt)
                for key, text in FILTER_LABELS.items()])
            flt_select.callback = self._filter
            self.add_item(flt_select)
        prev = discord.ui.Button(label="上一页", emoji="◀️", row=2, disabled=page <= 0)
        nxt = discord.ui.Button(label="下一页", emoji="▶️", row=2, disabled=page >= pages - 1)
        swap = discord.ui.Button(label="返回收藏柜" if in_bin else "查看回收站", emoji="🧸" if in_bin else "🗑️", row=2)
        prev.callback, nxt.callback, swap.callback = self._prev, self._next, self._swap
        for b in (prev, nxt, swap):
            self.add_item(b)

    async def _pick(self, interaction: discord.Interaction):
        await self.cog.show_detail(interaction, interaction.data["values"][0], self.flt, self.page)

    async def _filter(self, interaction: discord.Interaction):
        await self.cog.show_collection(interaction, interaction.data["values"][0], 0, edit=True)

    async def _prev(self, interaction: discord.Interaction):
        await self.cog.show_collection(interaction, self.flt, self.page - 1, edit=True)

    async def _next(self, interaction: discord.Interaction):
        await self.cog.show_collection(interaction, self.flt, self.page + 1, edit=True)

    async def _swap(self, interaction: discord.Interaction):
        await self.cog.show_collection(interaction, "all" if self.flt == "recycled" else "recycled", 0, edit=True)


class DetailView(discord.ui.View):
    def __init__(self, cog: ClawMachine, doll: dict, flt: str, page: int):
        super().__init__(timeout=600)
        self.cog, self.doll_id, self.flt, self.page = cog, doll["id"], flt, page
        self.showcased, self.favorite = doll["is_showcased"], bool(doll.get("is_favorite"))
        if doll.get("is_recycled"):
            restore = discord.ui.Button(label="恢复到收藏柜", emoji="♻️", style=discord.ButtonStyle.success)
            back = discord.ui.Button(label="返回回收站", emoji="↩️")
            restore.callback, back.callback = self._restore, self._back
            self.add_item(restore)
            self.add_item(back)
            return
        fav = discord.ui.Button(label="取消收藏" if self.favorite else "收藏", emoji="⭐",
                                style=discord.ButtonStyle.secondary if self.favorite else discord.ButtonStyle.primary)
        show = discord.ui.Button(label="取消展示" if self.showcased else "设为展示", emoji="🏆",
                                 style=discord.ButtonStyle.primary)
        give = discord.ui.Button(label="赠送", emoji="🎁", disabled=bool(doll["pending_gift"]))
        rec = discord.ui.Button(label="回收", emoji="🗑️", style=discord.ButtonStyle.danger,
                                disabled=bool(doll["pending_gift"]))
        back = discord.ui.Button(label="返回收藏柜", emoji="↩️")
        fav.callback, show.callback, give.callback = self._fav, self._show, self._give
        rec.callback, back.callback = self._recycle, self._back
        for b in (fav, show, give, rec, back):
            self.add_item(b)

    async def _fav(self, interaction: discord.Interaction):
        await self.cog.toggle_favorite(interaction, self.doll_id, not self.favorite, self.flt, self.page)

    async def _recycle(self, interaction: discord.Interaction):
        await self.cog.toggle_recycle(interaction, self.doll_id, True, self.flt, self.page)

    async def _restore(self, interaction: discord.Interaction):
        await self.cog.toggle_recycle(interaction, self.doll_id, False, self.flt, self.page)

    async def _show(self, interaction: discord.Interaction):
        await self.cog.toggle_showcase(interaction, self.doll_id, not self.showcased, self.flt, self.page)

    async def _give(self, interaction: discord.Interaction):
        await self.cog.open_gift_picker(interaction, self.doll_id, edit=True)

    async def _back(self, interaction: discord.Interaction):
        await self.cog.show_collection(interaction, self.flt, self.page, edit=True)


class GiftPickView(discord.ui.View):
    """选接收人 → 确认 → 发出邀请。"""

    def __init__(self, cog: ClawMachine, doll_id: str):
        super().__init__(timeout=300)
        self.cog, self.doll_id, self.target = cog, doll_id, None
        self.who = discord.ui.UserSelect(placeholder="送给谁？")
        self.who.callback = self._chosen
        self.add_item(self.who)

    async def _chosen(self, interaction: discord.Interaction):
        self.target = self.who.values[0]
        confirm = discord.ui.Button(label=f"确认送给 {self.target.display_name}"[:80], emoji="🎁",
                                    style=discord.ButtonStyle.success)
        cancel = discord.ui.Button(label="算了")
        confirm.callback, cancel.callback = self._confirm, self._cancel
        self.clear_items()
        self.add_item(confirm)
        self.add_item(cancel)
        await interaction.response.edit_message(view=self)

    async def _confirm(self, interaction: discord.Interaction):
        await self.cog.send_gift(interaction, self.doll_id, self.target)

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="已取消赠送。", embed=None, view=None)


class EggBookView(discord.ui.View):
    """图鉴：下拉只列出自己已发现的彩蛋（没发现的不出现在选项里，避免泄露称号）。"""

    def __init__(self, cog: ClawMachine, eggs: list[dict], mine: dict, back: bool = False):
        super().__init__(timeout=600)
        self.cog = cog
        found = [e for e in eggs if e["id"] in mine]
        if found:
            pick = discord.ui.Select(placeholder="查看名场面详情……", options=[
                discord.SelectOption(label=e["title"], value=e["id"], emoji=e.get("emoji") or "✨") for e in found])
            pick.callback = self._pick
            self.add_item(pick)
        if back:
            home = discord.ui.Button(label="返回图鉴", emoji="📖")
            home.callback = self._home
            self.add_item(home)
        firsts = discord.ui.Button(label="全服首发现", emoji="🏆")
        firsts.callback = self._firsts
        self.add_item(firsts)

    async def _pick(self, interaction: discord.Interaction):
        await self.cog.show_egg_detail(interaction, interaction.data["values"][0])

    async def _home(self, interaction: discord.Interaction):
        await self.cog.show_egg_book(interaction, edit=True)

    async def _firsts(self, interaction: discord.Interaction):
        await self.cog.show_discoveries(interaction, edit=True)


DYNAMIC_ITEMS = (GameButton, CollectionButton, GiveButton, DollButton, GiftButton)

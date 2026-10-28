"""不给糖就捣蛋：/trickortreat 玩家指令、/trickortreat_admin 管理指令、面板交互与公告补发。

所有私密结果都用 ephemeral；公开播报只走 outbox（搜查事务提交后才发，失败由后台循环补发）。
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..common import channel_allowed
from . import renderer as ui
from .constants import BODY_PARTS, CLOSED
from .service import StorageError, TotError, TotService
from .views import ConfirmView, hide_view, panel_view

log = logging.getLogger(__name__)

MSG_STORAGE = "活动数据暂时出了问题，请联系管理员。"
NO_PING = discord.AllowedMentions.none()
PART_CHOICES = [app_commands.Choice(name=text, value=pid) for pid, text in BODY_PARTS]


def is_manager(interaction: discord.Interaction) -> bool:
    p = interaction.permissions
    return bool(p.administrator or p.manage_guild)


async def say(interaction: discord.Interaction, text: str | None = None, **kw) -> None:
    kw.setdefault("allowed_mentions", NO_PING)
    kw.setdefault("ephemeral", True)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, **kw)
        else:
            await interaction.response.send_message(text, **kw)
    except discord.HTTPException as e:
        log.warning("不给糖就捣蛋：回复失败: %s", e)


def event_channels(event: dict) -> set[int]:
    return {int(event["entry_channel_id"]), int(event["announcement_channel_id"])}


class TrickOrTreat(commands.Cog, name="TrickOrTreat"):
    tot = app_commands.Group(name="trickortreat", description="🎃 不给糖就捣蛋 · 万圣夜寻糖", guild_only=True)
    admin = app_commands.Group(name="trickortreat_admin", description="（管理）不给糖就捣蛋活动管理",
                               guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: commands.Bot, service: TotService | None = None):
        self.bot = bot
        self.service = service or TotService()
        self.flush_locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        self.retry_loop.change_interval(seconds=self.service.cfg["outbox_retry_seconds"])
        self.retry_loop.start()

    async def cog_unload(self) -> None:
        self.retry_loop.cancel()

    # ------------------------------------------------------------------ 工具

    def _viewable_event(self, guild_id: int) -> dict | None:
        """状态 / 进度 / 奖品看当前轮；没有就看最近结束的一轮。"""
        cur = self.service.current_event(guild_id)
        if cur is not None:
            return cur
        events = self.service.store.load(guild_id)["events"]
        return max(events.values(), key=lambda e: int(e["id"][1:]), default=None)

    async def _channel(self, channel_id) -> discord.abc.Messageable | None:
        cid = int(channel_id)
        ch = self.bot.get_channel(cid)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(cid)
            except discord.HTTPException:
                return None
        return ch

    async def refresh_panel(self, guild_id: int, event: dict) -> None:
        if not event.get("panel_message_id"):
            return
        ch = await self._channel(event["panel_channel_id"])
        if ch is None:
            return
        try:
            msg = ch.get_partial_message(int(event["panel_message_id"]))
            await msg.edit(embed=ui.panel(event), view=panel_view(event["id"], closed=event["state"] == CLOSED))
        except discord.HTTPException as e:
            log.warning("不给糖就捣蛋：更新面板失败 guild=%s: %s", guild_id, e)

    # ------------------------------------------------------------------ 公告 outbox

    async def flush(self, guild_id: int) -> str | None:
        """按入队顺序发出待发公告；遇到失败就停（避免公开线索顺序颠倒）。返回给操作者的提示。"""
        async with self.flush_locks.setdefault(guild_id, asyncio.Lock()):
            try:
                pending = self.service.pending(guild_id)
            except StorageError:
                return MSG_STORAGE
            for event, item in pending:
                ch = await self._channel(event["announcement_channel_id"])
                try:
                    if ch is None:
                        raise RuntimeError("找不到公告频道")
                    if item["attempts"] > 0:
                        found = await self._already_sent(ch, item["id"])
                        if found is not None:
                            await self.service.mark_sent(guild_id, event["id"], item["id"], found)
                            continue
                    await self.service.mark_attempt(guild_id, event["id"], item["id"])
                    msg = await ch.send(embed=ui.announcement(item), allowed_mentions=NO_PING)
                except StorageError:
                    return MSG_STORAGE
                except (discord.HTTPException, RuntimeError) as e:
                    log.warning("不给糖就捣蛋：公告 %s 发送失败 guild=%s: %s", item["id"], guild_id, e)
                    try:
                        await self.service.mark_error(guild_id, event["id"], item["id"], str(e))
                    except StorageError:
                        pass
                    return "⚠️ 公开播报暂时没发出去（结果已保存，稍后会自动补发）。"
                try:
                    await self.service.mark_sent(guild_id, event["id"], item["id"], msg.id)
                except StorageError:
                    return MSG_STORAGE
        return None

    async def _already_sent(self, channel, outbox_id: str) -> int | None:
        """上次尝试可能「已发出但没来得及记录」：补发前在频道最近消息里找带同一公告编号的 Bot 消息。"""
        tag = f"公告 #{outbox_id}"
        try:
            async for m in channel.history(limit=self.service.cfg["history_check_limit"]):
                if m.author.id == self.bot.user.id and any(e.footer and e.footer.text == tag for e in m.embeds):
                    return m.id
        except discord.HTTPException as e:
            log.warning("不给糖就捣蛋：核对历史消息失败（%s），将直接补发", e)
        return None

    @tasks.loop(seconds=60)
    async def retry_loop(self):
        for gid in self.service.store.guild_ids_on_disk():
            try:
                if self.service.pending(gid):
                    await self.flush(gid)
            except StorageError:
                continue

    @retry_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    @retry_loop.error
    async def _loop_error(self, error: BaseException):
        log.exception("不给糖就捣蛋：公告补发循环异常", exc_info=error)

    # ------------------------------------------------------------------ 面板 / 报名

    async def handle_panel(self, interaction: discord.Interaction, op: str, eid: str):
        try:
            event = self.service.event(interaction.guild_id, eid)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        if event is None:
            return await say(interaction, "找不到这一轮活动。")
        if op == "join":
            return await self.offer_join(interaction, eid)
        if op == "status":
            return await say(interaction, ui.status(event, str(interaction.user.id)))
        await say(interaction, embed=ui.board(event))

    async def offer_join(self, interaction: discord.Interaction, eid: str):
        try:
            self.service.check_can_join(interaction.guild_id, eid, interaction.user.id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        except TotError as e:
            return await say(interaction, str(e))
        await say(interaction, "🎃 选一个藏糖部位（只有你自己看得到，选定后不能修改）：", view=hide_view(eid))

    async def handle_hide(self, interaction: discord.Interaction, eid: str, part: str):
        try:
            p = await self.service.join(interaction.guild_id, eid, interaction.user.id, part)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        except TotError as e:
            return await interaction.response.edit_message(content=str(e), view=None)
        log.info("不给糖就捣蛋：guild=%s 第 %s 轮新报名 user=%s", interaction.guild_id, eid, interaction.user.id)
        await interaction.response.edit_message(content=ui.join_ok(part, p["attempts_remaining"]), view=None)

    # ------------------------------------------------------------------ 玩家指令

    @tot.command(name="join", description="报名并藏好你的糖果（需在活动入口频道使用）")
    async def cmd_join(self, interaction: discord.Interaction):
        try:
            event = self.service.current_event(interaction.guild_id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        if event is None:
            return await say(interaction, "现在没有进行中的万圣夜活动。")
        if not channel_allowed(interaction.channel, {int(event["entry_channel_id"])}):
            return await say(interaction, f"请到活动入口 <#{event['entry_channel_id']}> 报名。")
        await self.offer_join(interaction, event["id"])

    @tot.command(name="search", description="消耗 1 次机会，公开搜查一位玩家的某个部位")
    @app_commands.describe(target="要搜查的玩家", part="搜查的部位")
    @app_commands.choices(part=PART_CHOICES)
    async def cmd_search(self, interaction: discord.Interaction, target: discord.Member,
                         part: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        gid = interaction.guild_id
        try:
            event = self.service.current_event(gid)
            if event is None:
                raise TotError("现在没有进行中的万圣夜活动。")
            if not channel_allowed(interaction.channel, event_channels(event)):
                raise TotError(f"请在活动频道 <#{event['announcement_channel_id']}> 搜查。")
            record, me, replay = await self.service.search(gid, interaction.user.id, target.id, part.value,
                                                           interaction.id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        except TotError as e:
            return await say(interaction, str(e))
        if not replay:
            log.info("不给糖就捣蛋：guild=%s 搜查 %s hit=%s", gid, record["id"], record["hit"])
        note = await self.flush(gid)
        total = event["config"]["attempts_per_player"]
        await say(interaction, ui.search_reply(record, me, total) + (f"\n{note}" if note else ""))

    @tot.command(name="status", description="查看我的藏糖位置、剩余次数和配对情况（仅自己可见）")
    async def cmd_status(self, interaction: discord.Interaction):
        try:
            event = self._viewable_event(interaction.guild_id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        if event is None:
            return await say(interaction, "本服还没有举办过万圣夜活动。")
        await say(interaction, ui.status(event, str(interaction.user.id)))

    @tot.command(name="board", description="查看公共进度与公开排除线索")
    async def cmd_board(self, interaction: discord.Interaction):
        try:
            event = self._viewable_event(interaction.guild_id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        if event is None:
            return await say(interaction, "本服还没有举办过万圣夜活动。")
        await say(interaction, embed=ui.board(event))

    @tot.command(name="prizes", description="查看我的奖品和全部奖品记录")
    async def cmd_prizes(self, interaction: discord.Interaction):
        try:
            event = self._viewable_event(interaction.guild_id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        if event is None:
            return await say(interaction, "本服还没有举办过万圣夜活动。")
        await say(interaction, ui.prizes(event, str(interaction.user.id)))

    @tot.command(name="withdraw", description="退出本轮活动（不能再搜查，也不会被搜）")
    async def cmd_withdraw(self, interaction: discord.Interaction):
        gid, uid = interaction.guild_id, interaction.user.id

        async def yes(i: discord.Interaction):
            try:
                await self.service.withdraw(gid, uid)
            except StorageError:
                return await i.response.edit_message(content=MSG_STORAGE, view=None)
            except TotError as e:
                return await i.response.edit_message(content=str(e), view=None)
            await i.response.edit_message(content="🚪 你已退出本轮活动。已经公开的搜查记录不会撤回；"
                                                  "如需恢复请联系管理员。", view=None)

        await say(interaction, "确定要退出本轮万圣夜活动吗？退出后不能自行重新报名，已用次数也不会恢复。",
                  view=ConfirmView(yes, "确认退出"))

    # ------------------------------------------------------------------ 管理指令

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if is_manager(interaction):
            return True
        await say(interaction, "只有拥有「管理服务器」权限的成员可以管理活动。")
        return False

    @admin.command(name="setup", description="设置报名入口频道和公开播报频道（可以相同）")
    async def adm_setup(self, interaction: discord.Interaction, entry_channel: discord.TextChannel,
                        announce_channel: discord.TextChannel):
        if not await self._guard(interaction):
            return
        try:
            await self.service.setup(interaction.guild_id, entry_channel.id, announce_channel.id, interaction.user.id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        await say(interaction, f"✅ 报名入口：{entry_channel.mention} · 公告频道：{announce_channel.mention}\n"
                               "-# 下一轮 open 时生效；搜查指令只能在这两个频道（及其子区）使用。")

    @admin.command(name="config", description="设置是否允许开局后补报（默认允许）")
    async def adm_config(self, interaction: discord.Interaction, allow_late_join: bool):
        if not await self._guard(interaction):
            return
        try:
            await self.service.set_late_join(interaction.guild_id, allow_late_join, interaction.user.id)
            event = self.service.current_event(interaction.guild_id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        if event is not None:
            await self.refresh_panel(interaction.guild_id, event)
        await say(interaction, f"✅ 开局后补报：{'允许' if allow_late_join else '不允许'}")

    @admin.command(name="open", description="开启新一轮报名，并在入口频道发布报名面板")
    async def adm_open(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        try:
            event = await self.service.open_event(interaction.guild_id, interaction.user.id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        except TotError as e:
            return await say(interaction, str(e))
        note = await self._post_panel(interaction.guild_id, event)
        await say(interaction, note or f"🎃 第 {event['id']} 轮已开放报名。准备好后用 `/trickortreat_admin start` 开始搜查。")

    @admin.command(name="panel", description="在入口频道重新发布本轮报名面板")
    async def adm_panel(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        try:
            event = self.service.current_event(interaction.guild_id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        if event is None:
            return await say(interaction, "现在没有未结束的活动。")
        await say(interaction, await self._post_panel(interaction.guild_id, event) or "✅ 面板已重新发布。")

    async def _post_panel(self, guild_id: int, event: dict) -> str | None:
        ch = await self._channel(event["entry_channel_id"])
        if ch is None:
            return "⚠️ 找不到报名入口频道，面板没发出；检查频道后用 `/trickortreat_admin panel` 重发。"
        try:
            msg = await ch.send(embed=ui.panel(event), view=panel_view(event["id"]), allowed_mentions=NO_PING)
            await self.service.set_panel(guild_id, event["id"], ch.id, msg.id)
        except discord.HTTPException as e:
            log.warning("不给糖就捣蛋：发布面板失败 guild=%s: %s", guild_id, e)
            return "⚠️ 面板发送失败（检查玉猫在入口频道的发言权限后用 `/trickortreat_admin panel` 重发）。"
        except StorageError:
            return MSG_STORAGE
        return None

    @admin.command(name="start", description="开始搜查阶段")
    async def adm_start(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        try:
            event = await self.service.start(interaction.guild_id, interaction.user.id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        except TotError as e:
            return await say(interaction, str(e))
        await self.refresh_panel(interaction.guild_id, event)
        ch = await self._channel(event["announcement_channel_id"])
        if ch is not None:
            try:
                await ch.send("🎃 **万圣夜寻糖开始了！** 用 `/trickortreat search` 搜查别人藏的糖果吧～",
                              allowed_mentions=NO_PING)
            except discord.HTTPException as e:
                log.warning("不给糖就捣蛋：开场公告失败: %s", e)
        await say(interaction, "✅ 已开始搜查阶段。")

    @admin.command(name="close", description="结束本轮：公布最终结果，之后不再接受报名和搜查")
    async def adm_close(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        gid = interaction.guild_id

        async def yes(i: discord.Interaction):
            try:
                event = await self.service.close(gid, i.user.id)
            except StorageError:
                return await i.response.edit_message(content=MSG_STORAGE, view=None)
            except TotError as e:
                return await i.response.edit_message(content=str(e), view=None)
            await i.response.edit_message(content="🔚 本轮已结束，正在公布结果……", view=None)
            await self.refresh_panel(gid, event)
            note = await self.flush(gid)
            await say(i, note or "✅ 最终结果已公布。")

        await say(interaction, "确定结束本轮万圣夜活动吗？结束后所有报名和搜查都会被拒绝。",
                  view=ConfirmView(yes, "结束本轮"))

    @admin.command(name="status", description="查看活动运行状态")
    async def adm_status(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        gid = interaction.guild_id
        try:
            text = ui.admin_status(self.service.current_event(gid), self.service.settings(gid),
                                   len(self.service.pending(gid)), len(self.service.failed(gid)))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        await say(interaction, text)

    @admin.command(name="repair-announcements", description="重试所有待发 / 发送失败的公开播报")
    async def adm_repair(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild_id
        try:
            requeued = await self.service.requeue_failed(gid, interaction.user.id)
            before = len(self.service.pending(gid))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        note = await self.flush(gid)
        try:
            left = len(self.service.pending(gid))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        text = f"📮 重新排队 {requeued} 条，本次发出 {before - left} 条，剩余 {left} 条。"
        await say(interaction, text + (f"\n{note}" if note else ""))

    @admin.command(name="restore", description="恢复一位已退出的玩家（不重置次数和藏糖位置）")
    async def adm_restore(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._guard(interaction):
            return
        try:
            await self.service.restore(interaction.guild_id, user.id, interaction.user.id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        except TotError as e:
            return await say(interaction, str(e))
        await say(interaction, f"✅ 已恢复 {user.mention} 的参与资格。")


__all__ = ["TrickOrTreat"]

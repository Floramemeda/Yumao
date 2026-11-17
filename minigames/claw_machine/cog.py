"""万象娃娃机：门票 Bot 消息监听、/claw 指令、按钮交互与赠礼过期。

门票 Bot 发出带 [YUNZHOU_CLAW_START] 的消息 → 玉猫按 EVENT_ID 开一局（同一事件只开一次）→ 回复一块游戏面板。
本 Cog 自己的 on_message 监听器只看可信门票 Bot 的消息，不受 bot.py 里「忽略 Bot」逻辑影响。
重启后会回扫游戏频道最近的消息，补开离线期间漏掉的门票；已经开过的 EVENT_ID 不会重复开。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..common import channel_allowed
from . import protocol
from . import renderer as ui
from .service import DONE, PENDING, ClawError, ClawService
from .store import StorageError
from .views import (CollectionView, DetailView, EggBookView, GiftPickView, aim_view, gift_view, pick_view,
                    result_view)

log = logging.getLogger(__name__)

MSG_STORAGE = "娃娃机的存档暂时出了问题，请联系管理员（你的抽奖券机会不会丢）。"
NO_PING = discord.AllowedMentions.none()


def ping(*user_ids) -> discord.AllowedMentions:
    return discord.AllowedMentions(everyone=False, roles=False, replied_user=False,
                                   users=[discord.Object(int(u)) for u in user_ids])


async def say(interaction: discord.Interaction, text: str, **kw) -> None:
    kw.setdefault("ephemeral", True)
    kw.setdefault("allowed_mentions", NO_PING)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, **kw)
        else:
            await interaction.response.send_message(text, **kw)
    except discord.HTTPException as e:
        log.warning("万象娃娃机：回复失败: %s", e)


class ClawMachine(commands.Cog, name="ClawMachine"):
    claw = app_commands.Group(name="claw", description="🧸 南域游乐场 · 万象娃娃机", guild_only=True)

    def __init__(self, bot: commands.Bot, service: ClawService | None = None):
        self.bot = bot
        self.service = service or ClawService()
        self.posting: set[tuple[int, str]] = set()      # 正在发面板的局，防止并发重复发
        self.announce_locks: dict[int, asyncio.Lock] = {}
        self._catch_up_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self.expire_loop.start()
        self.announce_loop.start()
        self._catch_up_task = asyncio.create_task(self.catch_up())

    async def cog_unload(self) -> None:
        self.expire_loop.cancel()
        self.announce_loop.cancel()
        if self._catch_up_task:
            self._catch_up_task.cancel()

    # ------------------------------------------------------------------ 门票监听

    def is_ticket_source(self, message: discord.Message) -> bool:
        cfg = self.service.cfg
        if message.guild is None or message.author.id not in set(cfg["trusted_bot_ids"]):
            return False
        return channel_allowed(message.channel, set(cfg["channel_ids"]))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ticket_source(message):
            return
        ticket = protocol.parse_message(message)
        if ticket is None:
            if protocol.MARKER in (message.content or ""):
                log.warning("万象娃娃机：门票消息格式不对，已忽略 message=%s", message.id)
            return
        await self.start_from_ticket(message, ticket)

    async def start_from_ticket(self, message: discord.Message, ticket: protocol.StartTicket) -> None:
        gid = message.guild.id
        try:
            game, created = await self.service.open_game(gid, message.channel.id, ticket.user_id, ticket.event_id,
                                                         message.id)
        except StorageError:
            log.error("万象娃娃机：开局写盘失败 event=%s", ticket.event_id)
            return
        key = (gid, game["id"])
        if game["status"] != PENDING or game["panel_message_id"] or key in self.posting:
            return                                     # 已开过且面板已发 / 已玩完 / 正在发
        if created:
            log.info("万象娃娃机：guild=%s 第 %s 局 user=%s event=%s", gid, game["id"], ticket.user_id, ticket.event_id)
        self.posting.add(key)
        try:
            others = len(self.service.pending_games(gid, ticket.user_id)) - 1
            msg = await message.channel.send(
                f"<@{ticket.user_id}>", embed=ui.pick_embed(self.service.cfg, game, others), view=pick_view(game),
                reference=message.to_reference(fail_if_not_exists=False), allowed_mentions=ping(ticket.user_id))
            await self.service.set_panel(gid, game["id"], msg.channel.id, msg.id)
        except discord.HTTPException as e:
            log.warning("万象娃娃机：面板发送失败 game=%s: %s（玩家可用 /claw resume）", game["id"], e)
        except StorageError:
            log.error("万象娃娃机：记录面板失败 game=%s", game["id"])
        finally:
            self.posting.discard(key)

    async def catch_up(self) -> None:
        """重启后回扫游戏频道，补上离线期间的门票。"""
        await self.bot.wait_until_ready()
        cfg = self.service.cfg
        after = discord.utils.utcnow() - timedelta(hours=cfg["catch_up_hours"])
        for cid in cfg["channel_ids"]:
            try:
                ch = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                async for m in ch.history(limit=cfg["catch_up_limit"], after=after, oldest_first=True):
                    if self.is_ticket_source(m):
                        ticket = protocol.parse_message(m)
                        if ticket is not None:
                            await self.start_from_ticket(m, ticket)
            except (discord.HTTPException, AttributeError) as e:
                log.warning("万象娃娃机：回扫频道 %s 失败: %s", cid, e)

    # ------------------------------------------------------------------ 游戏局按钮

    async def handle_game(self, interaction: discord.Interaction, op: str, game_id: str,
                          idx: int | None, tech: str | None) -> None:
        gid, cfg = interaction.guild_id, self.service.cfg
        try:
            game = self.service.own_game(gid, game_id, interaction.user.id)
            if game["status"] == DONE:
                return await self._show_result(interaction, game)
            if op == "go":
                if idx is None or tech is None:
                    raise ClawError("请重新选择目标和抓法。")
                return await self._grab(interaction, game_id, idx, tech)
            if op == "back" and idx is None:
                others = len(self.service.pending_games(gid, interaction.user.id)) - 1
                return await interaction.response.edit_message(embed=ui.pick_embed(cfg, game, others),
                                                               view=pick_view(game))
            if idx is None:
                raise ClawError("请重新选择目标。")
            self.service.check_choice(game, idx, tech if op == "tech" else None)
            t = tech if op == "tech" else None
            await interaction.response.edit_message(embed=ui.aim_embed(cfg, game, idx, t),
                                                    view=aim_view(cfg, game, idx, t))
        except ClawError as e:
            await say(interaction, str(e))
        except StorageError:
            await say(interaction, MSG_STORAGE)

    async def _show_result(self, interaction: discord.Interaction, game: dict) -> None:
        did = game["result"].get("doll_id")
        doll = self.service.doll(interaction.guild_id, did) if did else None
        await interaction.response.edit_message(content=None, embed=ui.result_embed(self.service.cfg, game, doll),
                                                view=result_view(doll))

    async def _grab(self, interaction: discord.Interaction, game_id: str, idx: int, tech: str) -> None:
        await interaction.response.defer()
        gid = interaction.guild_id
        game, doll, replay = await self.service.grab(gid, game_id, interaction.user.id, idx, tech)
        embed = ui.result_embed(self.service.cfg, game, doll)
        try:
            await interaction.edit_original_response(content=None, embed=embed, view=result_view(doll))
        except discord.HTTPException as e:
            log.warning("万象娃娃机：结果更新失败 game=%s: %s", game_id, e)
        if replay:
            return
        log.info("万象娃娃机：guild=%s 第 %s 局 %s%s", gid, game_id, "成功" if doll else "失败",
                 f" {doll['id']}" if doll else "")
        await self._publish_result(interaction, game, doll, embed)
        if game["result"].get("server_first"):
            await self.flush_announcements(gid)

    async def _publish_result(self, interaction, game: dict, doll: dict | None, embed: discord.Embed) -> None:
        """在私密界面（/claw resume）里抓的：把公开面板也改成结果；面板不在了就在游戏频道发一条。"""
        panel_id = int(game["panel_message_id"]) if game["panel_message_id"] else None
        if interaction.message is not None and panel_id == interaction.message.id:
            return
        if panel_id:
            ch = self.bot.get_channel(int(game["panel_channel_id"]))
            if ch is not None:
                try:
                    await ch.get_partial_message(panel_id).edit(content=None, embed=embed, view=result_view(doll))
                    return
                except discord.HTTPException:
                    pass
        ch = self.bot.get_channel(int(game["channel_id"]))
        if ch is not None:
            try:
                await ch.send(embed=embed, view=result_view(doll), allowed_mentions=NO_PING)
            except discord.HTTPException as e:
                log.warning("万象娃娃机：公开结果发送失败 game=%s: %s", game["id"], e)

    # ------------------------------------------------------------------ 全服首发现公告（outbox）

    async def flush_announcements(self, guild_id: int) -> None:
        """把待发的首发现公告发出去。首发现者早已在抓取事务里定好，这里只负责投递，失败了下次再试。

        每次发之前先记一次尝试；重试时（尝试次数 > 0）先在公告频道里按页脚标记找一下，
        找到说明上次其实发出去了（比如超时但 Discord 已收到），直接记成已发送，不重复发。
        """
        channel_id = self.service.cfg.get("announce_channel_id")
        if not channel_id:
            return
        lock = self.announce_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            try:
                pending = self.service.pending_announcements(guild_id)
            except StorageError:
                return
            if not pending:
                return
            ch = self.bot.get_channel(int(channel_id))
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(int(channel_id))
                except discord.HTTPException as e:
                    log.warning("万象娃娃机：找不到公告频道 %s: %s", channel_id, e)
                    return
            for rec in pending:
                egg_id = rec["egg_id"]
                doll = self.service.doll(guild_id, rec["doll_id"])
                if doll is None or not doll.get("egg"):
                    await self.service.finish_announcement(guild_id, egg_id, None, "娃娃记录缺失")
                    continue
                try:
                    if rec.get("announcement_attempts", 0) > 0:
                        found = await self._already_announced(ch, egg_id)
                        if found is not None:
                            await self.service.finish_announcement(guild_id, egg_id, found)
                            continue
                    await self.service.begin_announcement(guild_id, egg_id)
                    msg = await ch.send(embed=ui.announce_embed(rec, doll), allowed_mentions=NO_PING)
                except discord.HTTPException as e:
                    log.warning("万象娃娃机：首发现公告发送失败 egg=%s: %s", egg_id, e)
                    try:
                        await self.service.finish_announcement(guild_id, egg_id, None, str(e))
                    except StorageError:
                        pass
                    continue
                except StorageError:
                    return
                try:
                    await self.service.finish_announcement(guild_id, egg_id, msg.id)
                except StorageError:
                    log.error("万象娃娃机：公告已发出但记录失败 egg=%s message=%s（重试时会按页脚找到它）",
                              egg_id, msg.id)

    async def _already_announced(self, channel, egg_id: str) -> int | None:
        marker = ui.announce_footer(egg_id)
        me = self.bot.user.id if getattr(self.bot, "user", None) else None
        try:
            async for m in channel.history(limit=self.service.cfg["announce_history_check"]):
                if me is not None and m.author.id != me:
                    continue
                if any(e.footer and e.footer.text == marker for e in m.embeds):
                    return m.id
        except discord.HTTPException:
            pass
        return None

    @tasks.loop(minutes=2)
    async def announce_loop(self):
        for gid in self.service.store.guild_ids_on_disk():
            await self.flush_announcements(gid)

    @announce_loop.before_loop
    async def _announce_wait_ready(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ 收藏柜

    async def open_collection(self, interaction: discord.Interaction) -> None:
        await self.show_collection(interaction, "all", 0, edit=False)

    async def show_collection(self, interaction: discord.Interaction, flt: str, page: int, *, edit: bool,
                              note: str | None = None) -> None:
        try:
            dolls, page, pages = self.service.page(interaction.guild_id, interaction.user.id, flt, page)
            total = len(self.service.collection(interaction.guild_id, interaction.user.id, flt))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        embed = ui.collection_embed(interaction.user.id, dolls, flt, page, pages, total)
        view = CollectionView(self, dolls, flt, page, pages)
        if edit:
            await interaction.response.edit_message(content=note, embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def show_detail(self, interaction: discord.Interaction, doll_id: str, flt: str, page: int) -> None:
        doll = self.service.doll(interaction.guild_id, doll_id)
        if doll is None or doll["current_owner_id"] != str(interaction.user.id):
            return await say(interaction, "这只娃娃现在不在你手里。")
        await interaction.response.edit_message(content=None, embed=ui.detail_embed(doll),
                                                view=DetailView(self, doll, flt, page))

    async def toggle_showcase(self, interaction, doll_id: str, on: bool, flt: str, page: int) -> None:
        try:
            doll = await self.service.set_showcase(interaction.guild_id, interaction.user.id, doll_id, on)
        except ClawError as e:
            return await say(interaction, str(e))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        await interaction.response.edit_message(embed=ui.detail_embed(doll), view=DetailView(self, doll, flt, page))

    async def toggle_favorite(self, interaction, doll_id: str, on: bool, flt: str, page: int) -> None:
        try:
            doll = await self.service.set_favorite(interaction.guild_id, interaction.user.id, doll_id, on)
        except ClawError as e:
            return await say(interaction, str(e))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        await interaction.response.edit_message(content=None, embed=ui.detail_embed(doll),
                                                view=DetailView(self, doll, flt, page))

    async def toggle_recycle(self, interaction, doll_id: str, on: bool, flt: str, page: int) -> None:
        """回收后回到收藏柜列表（它已经不在里面了）；恢复后回到回收站列表。"""
        try:
            doll = await self.service.set_recycled(interaction.guild_id, interaction.user.id, doll_id, on)
        except ClawError as e:
            return await say(interaction, str(e))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        if on:
            note = f"🗑️ 已把 **{doll['name']}** 放进回收站，想要回来可以在「查看回收站」里恢复。"
            await self.show_collection(interaction, flt if flt != "recycled" else "all", page, edit=True, note=note)
        else:
            note = f"♻️ **{doll['name']}** 已恢复到收藏柜。"
            await self.show_collection(interaction, "recycled", page, edit=True, note=note)

    async def handle_doll_button(self, interaction: discord.Interaction, op: str, doll_id: str) -> None:
        """结算结果上的【收藏】【回收】：只有娃娃现在的主人能点；点完只改这条消息的按钮，不动结果内容。"""
        doll = self.service.doll(interaction.guild_id, doll_id)
        if doll is None or doll["current_owner_id"] != str(interaction.user.id):
            return await say(interaction, "这只娃娃现在不在你手里。")
        try:
            if op == "fav":
                doll = await self.service.set_favorite(interaction.guild_id, interaction.user.id, doll_id,
                                                       not doll.get("is_favorite"))
            elif doll.get("is_recycled"):
                return await say(interaction, "这只娃娃已经在回收站里了，可以在 `/claw collection` →「查看回收站」里恢复。")
            else:
                doll = await self.service.set_recycled(interaction.guild_id, interaction.user.id, doll_id, True)
        except ClawError as e:
            return await say(interaction, str(e))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        await interaction.response.edit_message(view=result_view(doll))
        if op == "fav":
            text = "⭐ 已收藏，它会排在收藏柜最前面。" if doll["is_favorite"] else "已取消收藏。"
        else:
            text = "🗑️ 已放进回收站，不会再出现在收藏柜里；想要回来可以在 `/claw collection` →「查看回收站」里恢复。"
        await interaction.followup.send(text, ephemeral=True)

    # ------------------------------------------------------------------ 赠送

    async def open_gift_picker(self, interaction: discord.Interaction, doll_id: str, *, edit: bool = False) -> None:
        doll = self.service.doll(interaction.guild_id, doll_id)
        if doll is None or doll["current_owner_id"] != str(interaction.user.id):
            return await say(interaction, "这只娃娃现在不在你手里。")
        if doll.get("is_recycled"):
            return await say(interaction, "这只娃娃在回收站里，先恢复到收藏柜再送。")
        if doll["pending_gift"]:
            gift = self.service.gift(interaction.guild_id, doll["pending_gift"])
            if gift and gift["status"] == "pending":
                return await say(interaction, "这只娃娃已经有一份待对方确认的赠礼了。")
        kw = dict(content="选择要送的人（对方收下后才会转交）：", embed=ui.detail_embed(doll),
                  view=GiftPickView(self, doll_id))
        if edit:
            await interaction.response.edit_message(**kw)
        else:
            await interaction.response.send_message(**kw, ephemeral=True)

    async def send_gift(self, interaction: discord.Interaction, doll_id: str, target) -> None:
        gid = interaction.guild_id
        try:
            gift = await self.service.create_gift(gid, interaction.user.id, doll_id, target.id,
                                                  getattr(target, "bot", False))
        except ClawError as e:
            return await interaction.response.edit_message(content=f"⚠️ {e}", embed=None, view=None)
        except StorageError:
            return await interaction.response.edit_message(content=MSG_STORAGE, embed=None, view=None)
        doll = self.service.doll(gid, doll_id)
        try:
            msg = await interaction.channel.send(f"<@{target.id}>", embed=ui.gift_invite_embed(gift, doll),
                                                 view=gift_view(gift["id"]), allowed_mentions=ping(target.id))
            await self.service.set_gift_message(gid, gift["id"], msg.channel.id, msg.id)
        except (discord.HTTPException, AttributeError) as e:
            log.warning("万象娃娃机：赠礼邀请发送失败 gift=%s: %s", gift["id"], e)
            await self.service.cancel_gift_draft(gid, gift["id"])
            return await interaction.response.edit_message(
                content="⚠️ 邀请发不出去（玉猫可能没有这个频道的发言权限），娃娃还在你手里。", embed=None, view=None)
        await interaction.response.edit_message(
            content=f"✅ 邀请已发出！{target.mention} 收下前这只娃娃会被锁定，"
                    f"{self.service.cfg['gift_expire_hours']} 小时没回应会自动退回。", embed=None, view=None)

    async def handle_gift(self, interaction: discord.Interaction, gift_id: str, op: str) -> None:
        try:
            gift, doll, changed = await self.service.respond_gift(interaction.guild_id, gift_id,
                                                                  interaction.user.id, op)
        except ClawError as e:
            return await say(interaction, str(e))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        await interaction.response.edit_message(content=None, embed=ui.gift_done_embed(gift, doll), view=None)
        if not changed:
            await say(interaction, "这份赠礼已经处理过了。")

    @tasks.loop(minutes=10)
    async def expire_loop(self):
        for gid in self.service.store.guild_ids_on_disk():
            try:
                expired = await self.service.expire_gifts(gid)
            except StorageError:
                continue
            for gift in expired:
                if not gift.get("message_id"):
                    continue
                ch = self.bot.get_channel(int(gift["channel_id"]))
                if ch is None:
                    continue
                doll = self.service.doll(gid, gift["doll_id"])
                try:
                    await ch.get_partial_message(int(gift["message_id"])).edit(
                        content=None, embed=ui.gift_done_embed(gift, doll), view=None)
                except discord.HTTPException:
                    pass

    @expire_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ 指令

    @claw.command(name="resume", description="继续还没玩完的娃娃机（抽奖券机会不会丢）")
    async def cmd_resume(self, interaction: discord.Interaction):
        try:
            games = self.service.pending_games(interaction.guild_id, interaction.user.id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        if not games:
            return await say(interaction, "你没有待玩的娃娃机。用好运无限抽奖券就能开一局～")
        game = games[0]
        await interaction.response.send_message(embed=ui.pick_embed(self.service.cfg, game, len(games) - 1),
                                                view=pick_view(game), ephemeral=True)

    @claw.command(name="collection", description="打开我的娃娃收藏柜（仅自己可见）")
    async def cmd_collection(self, interaction: discord.Interaction):
        await self.open_collection(interaction)

    @claw.command(name="showcase", description="看看谁的娃娃展示柜")
    @app_commands.describe(user="要看的人（默认自己）")
    @app_commands.rename(user="玩家")
    async def cmd_showcase(self, interaction: discord.Interaction, user: discord.Member | None = None):
        who = user or interaction.user
        try:
            dolls = self.service.showcase(interaction.guild_id, who.id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        await interaction.response.send_message(
            embed=ui.showcase_embed(who.id, dolls, self.service.cfg["max_showcase"]), allowed_mentions=NO_PING)

    # ------------------------------------------------------------------ 名场面图鉴

    async def show_egg_book(self, interaction: discord.Interaction, *, edit: bool = False) -> None:
        try:
            mine = self.service.discoveries(interaction.guild_id, interaction.user.id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        eggs = self.service.cfg["_eggs"]
        kw = dict(embed=ui.egg_book_embed(interaction.user.id, eggs, mine), view=EggBookView(self, eggs, mine))
        if edit:
            await interaction.response.edit_message(**kw)
        else:
            await interaction.response.send_message(**kw, ephemeral=True)

    async def show_egg_detail(self, interaction: discord.Interaction, egg_id: str) -> None:
        mine = self.service.discoveries(interaction.guild_id, interaction.user.id)
        egg = next((e for e in self.service.cfg["_eggs"] if e["id"] == egg_id), None)
        if egg is None or egg_id not in mine:                  # 没亲手发现的不给看详情
            return await say(interaction, "这一页还是 🔒 ？？？")
        first = self.service.first_discoveries(interaction.guild_id).get(egg_id)
        await interaction.response.edit_message(embed=ui.egg_detail_embed(egg, first, mine[egg_id]),
                                                view=EggBookView(self, self.service.cfg["_eggs"], mine, back=True))

    async def show_discoveries(self, interaction: discord.Interaction, *, edit: bool = False) -> None:
        try:
            firsts = self.service.first_discoveries(interaction.guild_id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        embed = ui.discoveries_embed(self.service.cfg["_eggs"], firsts)
        if edit:
            mine = self.service.discoveries(interaction.guild_id, interaction.user.id)
            await interaction.response.edit_message(
                embed=embed, view=EggBookView(self, self.service.cfg["_eggs"], mine, back=True))
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @claw.command(name="eggs", description="📖 云州名场面图鉴：看看你亲手发现了哪些隐藏彩蛋（仅自己可见）")
    async def cmd_eggs(self, interaction: discord.Interaction):
        await self.show_egg_book(interaction)

    @claw.command(name="discoveries", description="🏆 全服首发现：每种隐藏彩蛋是谁最先抓到的（仅自己可见）")
    async def cmd_discoveries(self, interaction: discord.Interaction):
        await self.show_discoveries(interaction)

    @claw.command(name="stats", description="娃娃机战绩")
    @app_commands.describe(user="要看的人（默认自己）")
    @app_commands.rename(user="玩家")
    async def cmd_stats(self, interaction: discord.Interaction, user: discord.Member | None = None):
        who = user or interaction.user
        try:
            st = self.service.stats(interaction.guild_id, who.id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        await interaction.response.send_message(embed=ui.stats_embed(who.id, st), ephemeral=True)


__all__ = ["ClawMachine"]

"""RP 骰点与场次助手：自动骰点监听、/roll、/roll-config、/rp 与 DM 私密控制台。

DM 控制台是临时的 ephemeral 视图：组件只带 session id / 玩家 id，每次操作都在服务端重新校验
场次状态与操作者权限；面板过期或 Bot 重启后再用 /rp dm 打开即可。
"""
from __future__ import annotations

import collections
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from . import dice
from . import presentation as ui
from .service import Actor, RpError, RpService, StorageError

log = logging.getLogger(__name__)

MSG_STORAGE = "RP 数据暂时出了问题，请联系管理员。"
PANEL_TIMEOUT = 840   # ephemeral 交互令牌 15 分钟过期，面板略早失效


def is_admin(interaction: discord.Interaction) -> bool:
    p = interaction.permissions
    return bool(p.administrator or p.manage_guild)


def actor_of(interaction: discord.Interaction) -> Actor:
    return Actor(interaction.user.id, is_admin(interaction))


async def say(interaction: discord.Interaction, text: str, *, ephemeral: bool = True, **kw) -> None:
    kw.setdefault("allowed_mentions", discord.AllowedMentions.none())
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=ephemeral, **kw)
        else:
            await interaction.response.send_message(text, ephemeral=ephemeral, **kw)
    except discord.HTTPException as e:
        log.warning("RP：回复失败: %s", e)


# ---------------------------------------------------------------- Modal

class StatModal(discord.ui.Modal, title="修改数值"):
    def __init__(self, cog: RpDice, session_id: str, user_id: str):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog, self.session_id, self.user_id = cog, session_id, user_id
        hint = "如 -15 / +20 表示增减，80 表示设为 80；留空不改"
        self.sanity = discord.ui.TextInput(label="理智", required=False, max_length=8, placeholder=hint)
        self.heat = discord.ui.TextInput(label="发情值", required=False, max_length=8, placeholder=hint)
        self.add_item(self.sanity)
        self.add_item(self.heat)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.op_stats(interaction, self.session_id, self.user_id,
                                {"sanity": self.sanity.value, "heat": self.heat.value})


class ConditionModal(discord.ui.Modal, title="添加异常状态"):
    def __init__(self, cog: RpDice, session_id: str, user_id: str):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog, self.session_id, self.user_id = cog, session_id, user_id
        self.name = discord.ui.TextInput(label="异常名称", max_length=cog.service.cfg["condition_max_length"],
                                         placeholder="例：束缚 I")
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.op_condition(interaction, self.session_id, self.user_id, add=self.name.value)


class ProgressModal(discord.ui.Modal, title="设置探索进度"):
    def __init__(self, cog: RpDice, session_id: str, progress_max: int):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog, self.session_id = cog, session_id
        self.value = discord.ui.TextInput(label=f"当前进度（0–{progress_max}）", max_length=4)
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction):
        v = self.value.value.strip()
        if not v.isdigit():
            return await say(interaction, "请输入整数。")
        await self.cog.op_progress(interaction, self.session_id, set_to=int(v))


# ---------------------------------------------------------------- 控制台视图

class DmPanel(discord.ui.View):
    def __init__(self, cog: RpDice, session: dict, names: dict[str, str]):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog, self.sid = cog, session["id"]
        self.progress_max = session["progress_max"]

        add = discord.ui.UserSelect(placeholder="➕ 加入玩家…", min_values=1, max_values=10, row=0)
        add.callback = self.on_add
        self.add_item(add)

        players = RpService.active_players(session)
        if players:
            pick = discord.ui.Select(placeholder="🎯 选择要操作的玩家…", row=1, options=[
                discord.SelectOption(label=names.get(uid, uid)[:100], value=uid,
                                     description=f"理智 {p['sanity']} · 发情值 {p['heat']}")
                for uid, p in players[:25]])
            pick.callback = self.on_pick
            self.add_item(pick)

        for label, delta, style in (("进度 -1", -1, discord.ButtonStyle.secondary),
                                    ("进度 +1", 1, discord.ButtonStyle.primary)):
            b = discord.ui.Button(label=label, style=style, row=2)
            b.callback = (lambda d: (lambda i: self.cog.op_progress(i, self.sid, delta=d)))(delta)
            self.add_item(b)
        setp = discord.ui.Button(label="设置进度", style=discord.ButtonStyle.secondary, row=2)
        setp.callback = lambda i: i.response.send_modal(ProgressModal(self.cog, self.sid, self.progress_max))
        self.add_item(setp)
        refresh = discord.ui.Button(label="刷新", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
        refresh.callback = lambda i: self.cog.show_panel(i, self.sid)
        self.add_item(refresh)
        end = discord.ui.Button(label="结束探索", emoji="🏁", style=discord.ButtonStyle.danger, row=3)
        end.callback = lambda i: self.cog.confirm_end(i, self.sid)
        self.add_item(end)

    async def on_add(self, interaction: discord.Interaction):
        ids = [int(v) for v in interaction.data.get("values", [])]
        await self.cog.op_add_players(interaction, self.sid, ids)

    async def on_pick(self, interaction: discord.Interaction):
        await self.cog.show_player(interaction, self.sid, interaction.data["values"][0])


class PlayerPanel(discord.ui.View):
    def __init__(self, cog: RpDice, session_id: str, user_id: str, player: dict):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog, self.sid, self.uid = cog, session_id, user_id

        stats = discord.ui.Button(label="修改理智 / 发情值", emoji="📊", style=discord.ButtonStyle.primary, row=0)
        stats.callback = lambda i: i.response.send_modal(StatModal(self.cog, self.sid, self.uid))
        self.add_item(stats)
        cond = discord.ui.Button(label="添加异常", emoji="➕", style=discord.ButtonStyle.secondary, row=0)
        cond.callback = lambda i: i.response.send_modal(ConditionModal(self.cog, self.sid, self.uid))
        self.add_item(cond)
        if player["conditions"]:
            rm = discord.ui.Select(placeholder="➖ 移除异常…", row=1, options=[
                discord.SelectOption(label=c["name"][:100], value=c["name"][:100]) for c in player["conditions"][:25]])
            rm.callback = lambda i: self.cog.op_condition(i, self.sid, self.uid, remove=i.data["values"][0])
            self.add_item(rm)
        kick = discord.ui.Button(label="移出本场", style=discord.ButtonStyle.danger, row=2)
        kick.callback = lambda i: self.cog.confirm_remove(i, self.sid, self.uid)
        self.add_item(kick)
        back = discord.ui.Button(label="返回", style=discord.ButtonStyle.secondary, row=2)
        back.callback = lambda i: self.cog.show_panel(i, self.sid)
        self.add_item(back)


class Confirm(discord.ui.View):
    def __init__(self, on_yes, on_no, yes_label: str):
        super().__init__(timeout=120)
        yes = discord.ui.Button(label=yes_label, style=discord.ButtonStyle.danger)
        yes.callback = on_yes
        no = discord.ui.Button(label="取消", style=discord.ButtonStyle.secondary)
        no.callback = on_no
        self.add_item(yes)
        self.add_item(no)


# ---------------------------------------------------------------- Cog

class RpDice(commands.Cog, name="RpDice"):
    rp = app_commands.Group(name="rp", description="RP 探索场次", guild_only=True)
    roll_config = app_commands.Group(name="roll-config", description="（管理）自动骰点频道", guild_only=True,
                                     default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: commands.Bot, service: RpService | None = None, clock=time.monotonic):
        self.bot = bot
        self.service = service or RpService()
        self.clock = clock
        self.last_roll: dict[tuple[int, int], float] = {}
        self.seen = collections.deque(maxlen=1000)    # 最近处理过的消息 id：防重复投递

    # ------------------------------------------------------------------ 骰点

    def _rate_limited(self, user_id: int, channel_id: int) -> bool:
        now = self.clock()
        key = (user_id, channel_id)
        if now - self.last_roll.get(key, -1e9) < self.service.cfg["roll_cooldown_seconds"]:
            return True
        self.last_roll[key] = now
        if len(self.last_roll) > 5000:
            cutoff = now - 60
            self.last_roll = {k: t for k, t in self.last_roll.items() if t > cutoff}
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        try:
            expr = dice.parse(message.content, self.service.cfg["dice_limits"])
        except dice.DiceError:
            return                                   # 超限：静默忽略，避免打扰聊天
        if expr is None:
            return
        try:
            if not self.service.is_roll_channel(message.guild.id, message.channel.id,
                                                getattr(message.channel, "parent_id", None)):
                return
        except StorageError:
            return
        if message.id in self.seen or self._rate_limited(message.author.id, message.channel.id):
            return
        self.seen.append(message.id)
        result = dice.roll(expr)
        try:
            await message.reply(dice.format_result(result), mention_author=False,
                                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("RP：骰点回复失败 channel=%s: %s", message.channel.id, e)

    @app_commands.command(name="roll", description="掷骰，例：1d100>98、2d6、1d20>=15")
    @app_commands.describe(expression="骰式，如 1d100>98")
    @app_commands.guild_only()
    async def roll(self, interaction: discord.Interaction, expression: str):
        try:
            expr = dice.parse(expression, self.service.cfg["dice_limits"])
        except dice.DiceError as e:
            return await say(interaction, str(e))
        if expr is None:
            return await say(interaction, "骰式格式不对，例：`1d100>98`、`2d6`、`1d20>=15`。")
        if self._rate_limited(interaction.user.id, interaction.channel_id):
            return await say(interaction, "掷得太快啦，稍等一下。")
        result = dice.roll(expr)
        await interaction.response.send_message(f"{interaction.user.mention} {dice.format_result(result)}",
                                                allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------ /roll-config

    async def _config_guard(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await say(interaction, "只有拥有「管理服务器」权限的管理员可以配置。")
            return False
        return True

    @roll_config.command(name="add", description="开启频道（及其子区）的自动骰点识别")
    async def config_add(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if await self._config_guard(interaction):
            await self._set_channel(interaction, channel, True)

    @roll_config.command(name="remove", description="关闭频道的自动骰点识别")
    async def config_remove(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if await self._config_guard(interaction):
            await self._set_channel(interaction, channel, False)

    async def _set_channel(self, interaction, channel, enabled: bool):
        if channel.id in self.service.config_roll_channels(interaction.guild_id):
            return await say(interaction, f"{channel.mention} 写在配置文件里，固定开启；要关闭需要修改 config.json。")
        try:
            changed = await self.service.set_roll_channel(interaction.guild_id, channel.id, enabled,
                                                          interaction.user.id)
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        state = "开启" if enabled else "关闭"
        await say(interaction, f"已{state} {channel.mention} 的自动骰点。" if changed
                  else f"{channel.mention} 本来就是{state}状态。")

    @roll_config.command(name="list", description="查看本服开启自动骰点的频道")
    async def config_list(self, interaction: discord.Interaction):
        if not await self._config_guard(interaction):
            return
        fixed = sorted(self.service.config_roll_channels(interaction.guild_id))
        try:
            added = [c for c in self.service.roll_channels(interaction.guild_id) if c not in fixed]
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        lines = [f"• <#{c}>（配置文件，固定开启）" for c in fixed] + [f"• <#{c}>" for c in added]
        await say(interaction, "自动骰点频道（子区自动继承）：\n" + "\n".join(lines)
                  if lines else "本服还没有开启自动骰点的频道。")

    # ------------------------------------------------------------------ /rp

    def can_start(self, interaction: discord.Interaction) -> bool:
        roles = {r.id for r in getattr(interaction.user, "roles", [])}
        dm_roles = self.service.dm_role_ids(interaction.guild_id)
        return bool(roles & dm_roles) or interaction.permissions.manage_channels or is_admin(interaction)

    @rp.command(name="start", description="在当前频道/子区开始一场探索，你将成为 DM")
    @app_commands.describe(progress_max="总进度格数（默认 10）")
    async def rp_start(self, interaction: discord.Interaction,
                       progress_max: app_commands.Range[int, 1, 100] | None = None):
        if not self.can_start(interaction):
            return await say(interaction, "只有 DM 身份组或拥有「管理频道」权限的人可以开始探索。")
        try:
            s = await self.service.start(interaction.guild_id, interaction.channel_id, interaction.user.id,
                                         progress_max)
        except RpError as e:
            return await say(interaction, str(e))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        await interaction.response.send_message(ui.start_message(s), allowed_mentions=discord.AllowedMentions.none())
        embed, view = await self._panel(interaction, s)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @rp.command(name="dm", description="打开本频道探索的 DM 控制台（仅 DM / 管理员）")
    async def rp_dm(self, interaction: discord.Interaction):
        try:
            s = self.service.require_active(interaction.guild_id, interaction.channel_id)
        except RpError as e:
            return await say(interaction, str(e))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        if not self.service.can_manage(s, actor_of(interaction)):
            return await say(interaction, "只有本场 DM 或管理员可以打开控制台。")
        await interaction.response.defer(ephemeral=True, thinking=True)
        note = await self.flush(interaction.guild_id, s["id"])   # 顺便补发之前失败的公告
        s = self.service.require_active(interaction.guild_id, interaction.channel_id)
        embed, view = await self._panel(interaction, s, note)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @rp.command(name="status", description="公开查看本频道探索的队伍状态")
    async def rp_status(self, interaction: discord.Interaction):
        try:
            s = self.service.require_active(interaction.guild_id, interaction.channel_id)
        except RpError as e:
            return await say(interaction, str(e))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        embeds = ui.status_embeds(s, RpService.active_players(s))
        none = discord.AllowedMentions.none()
        await interaction.response.send_message(embeds=embeds[:10], allowed_mentions=none)
        for i in range(10, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[i:i + 10], allowed_mentions=none)

    @rp.command(name="end", description="结束本频道的探索（仅 DM / 管理员，需确认）")
    async def rp_end(self, interaction: discord.Interaction):
        try:
            s = self.service.require_active(interaction.guild_id, interaction.channel_id)
        except RpError as e:
            return await say(interaction, str(e))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        await self.confirm_end(interaction, s["id"])

    # ------------------------------------------------------------------ 面板

    async def _names(self, interaction: discord.Interaction, session: dict) -> dict[str, str]:
        names = {}
        for uid, _ in RpService.active_players(session):
            m = interaction.guild.get_member(int(uid)) if interaction.guild else None
            names[uid] = m.display_name if m else f"用户{uid}"
        return names

    async def _panel(self, interaction, session, note=None):
        players = RpService.active_players(session)
        return (ui.dm_panel_embed(session, players, note),
                DmPanel(self, session, await self._names(interaction, session)))

    def _session_by_id(self, guild_id: int, session_id: str) -> dict:
        s = self.service.store.load(guild_id)["sessions"].get(session_id)
        if s is None:
            raise RpError("找不到这场探索。")
        return s

    async def show_panel(self, interaction: discord.Interaction, session_id: str, note: str | None = None):
        try:
            s = self._session_by_id(interaction.guild_id, session_id)
            if s["status"] != "ACTIVE":
                return await interaction.response.edit_message(content="这场探索已经结束了。", embed=None, view=None)
            embed, view = await self._panel(interaction, s, note)
        except RpError as e:
            return await say(interaction, str(e))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    async def show_player(self, interaction: discord.Interaction, session_id: str, user_id: str,
                          note: str | None = None):
        try:
            s = self._session_by_id(interaction.guild_id, session_id)
            p = s["participants"].get(user_id)
            if p is None or not p["active"] or s["status"] != "ACTIVE":
                return await self.show_panel(interaction, session_id, "这位玩家已经不在本场了。")
        except RpError as e:
            return await say(interaction, str(e))
        except StorageError:
            return await say(interaction, MSG_STORAGE)
        await interaction.response.edit_message(embed=ui.player_panel_embed(user_id, p, note),
                                                view=PlayerPanel(self, session_id, user_id, p))

    # ------------------------------------------------------------------ 操作

    async def _run(self, interaction: discord.Interaction, coro) -> tuple[bool, object]:
        """执行一次修改；失败时在原面板上提示（不改状态）。"""
        try:
            return True, await coro
        except RpError as e:
            await say(interaction, str(e))
        except StorageError:
            await say(interaction, MSG_STORAGE)
        return False, None

    async def op_add_players(self, interaction, session_id: str, user_ids: list[int]):
        resolved = ((interaction.data or {}).get("resolved") or {}).get("users", {})
        bots = {int(k) for k, u in resolved.items() if u.get("bot")}     # 机器人不能当玩家
        ok, added = await self._run(interaction, self.service.add_players(
            interaction.guild_id, session_id, actor_of(interaction), [u for u in user_ids if u not in bots]))
        if ok:
            note = await self.flush(interaction.guild_id, session_id)
            text = f"已加入 {len(added)} 名玩家。" if added else "所选玩家都已在场。"
            await self.show_panel(interaction, session_id, text + (f"\n{note}" if note else ""))

    async def op_stats(self, interaction, session_id: str, user_id: str, inputs: dict):
        ok, res = await self._run(interaction, self.service.change_stats(
            interaction.guild_id, session_id, actor_of(interaction), int(user_id), inputs))
        if ok:
            note = await self.flush(interaction.guild_id, session_id)
            text = "已更新并公告。" if res else "数值没有变化，不发公告。"
            await self.show_player(interaction, session_id, user_id, text + (f"\n{note}" if note else ""))

    async def op_condition(self, interaction, session_id: str, user_id: str, *, add: str | None = None,
                           remove: str | None = None):
        actor = actor_of(interaction)
        coro = (self.service.add_condition(interaction.guild_id, session_id, actor, int(user_id), add) if add is not None
                else self.service.remove_condition(interaction.guild_id, session_id, actor, int(user_id), remove))
        ok, _ = await self._run(interaction, coro)
        if ok:
            note = await self.flush(interaction.guild_id, session_id)
            await self.show_player(interaction, session_id, user_id, "已更新并公告。" + (f"\n{note}" if note else ""))

    async def op_progress(self, interaction, session_id: str, *, delta: int | None = None, set_to: int | None = None):
        ok, res = await self._run(interaction, self.service.change_progress(
            interaction.guild_id, session_id, actor_of(interaction), delta=delta, set_to=set_to))
        if ok:
            note = await self.flush(interaction.guild_id, session_id)
            text = "进度已更新。" if res else "进度没有变化。"
            await self.show_panel(interaction, session_id, text + (f"\n{note}" if note else ""))

    async def confirm_remove(self, interaction, session_id: str, user_id: str):
        async def yes(i):
            ok, _ = await self._run(i, self.service.remove_player(i.guild_id, session_id, actor_of(i), int(user_id)))
            if ok:
                await self.show_panel(i, session_id, f"已将 <@{user_id}> 移出本场（记录保留）。")

        await interaction.response.edit_message(
            content=f"确定把 <@{user_id}> 移出本场吗？其状态与历史记录会保留。", embed=None,
            view=Confirm(yes, lambda i: self.show_player(i, session_id, user_id), "确认移出"))

    async def confirm_end(self, interaction, session_id: str):
        try:
            s = self._session_by_id(interaction.guild_id, session_id)
        except (RpError, StorageError) as e:
            return await say(interaction, str(e) if isinstance(e, RpError) else MSG_STORAGE)
        if not self.service.can_manage(s, actor_of(interaction)):
            return await say(interaction, "只有本场 DM 或管理员可以结束探索。")

        async def yes(i):
            ok, _ = await self._run(i, self.service.end(i.guild_id, session_id, actor_of(i)))
            if ok:
                note = await self.flush(i.guild_id, session_id)
                await i.response.edit_message(content="🏁 探索已结束。" + (f"\n{note}" if note else ""),
                                              embed=None, view=None)

        async def no(i):
            await i.response.edit_message(content="已取消。", embed=None, view=None)

        view = Confirm(yes, no, "确认结束")
        if interaction.response.is_done() or interaction.type == discord.InteractionType.application_command:
            await say(interaction, "确定要结束本场探索吗？结束后不能再修改。", view=view)
        else:
            await interaction.response.edit_message(content="确定要结束本场探索吗？结束后不能再修改。",
                                                    embed=None, view=view)

    # ------------------------------------------------------------------ 公告

    async def flush(self, guild_id: int, session_id: str) -> str | None:
        """依次发出待发公告；失败的保留到下次。返回给 DM 的提示（全部成功则 None）。"""
        try:
            pending = self.service.pending(guild_id, session_id)
            if not pending:
                return None
            s = self.service.store.load(guild_id)["sessions"][session_id]
        except StorageError:
            return MSG_STORAGE
        channel = self.bot.get_channel(int(s["channel_id"]))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(s["channel_id"]))
            except discord.HTTPException:
                channel = None
        for ann in pending:
            if channel is None:
                return "⚠️ 找不到场次频道，公告没发出（状态已保存，下次操作或 /rp dm 时会补发）。"
            try:
                msg = await channel.send(ui.announcement(s, ann), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                log.warning("RP：公告发送失败 session=%s: %s", session_id, e)
                return "⚠️ 公告发送失败（状态已保存，下次操作或 /rp dm 时会补发）。"
            try:
                await self.service.mark_announced(guild_id, session_id, ann["event_id"], msg.id)
            except StorageError:
                return MSG_STORAGE
        return None

"""皮套系统：Slash Command、消息右键菜单、Modal / Select 与发送流程。

长文保护：/say 与「以皮套回复」的正文 Modal 不靠内存里的 Modal 对象处理，
而是在 on_interaction 里按 custom_id 统一处理——Bot 重启后提交的 Modal 也能被收下、落盘、发送。
"""
from __future__ import annotations

import asyncio
import logging
import secrets

import discord
from discord import app_commands
from discord.ext import commands

from . import presentation as ui
from .service import FAILED, PARTIAL, SENDING, SENT, UNKNOWN, PersonaError, PersonaService, StorageError
from .webhook_sender import KNOWN_WEBHOOK_IDS, SendFailed, SendUnknown, WebhookSender, check_channel

log = logging.getLogger(__name__)

MSG_STORAGE = "皮套数据暂时出了问题，请联系管理员。"
DRAFT_HINT = "\n草稿 `{id}` 已保存：在原频道用 `/say-retry` 重发，或用 `/say-draft` 查看原文。"
PAGE = 25


def role_ids(interaction: discord.Interaction) -> list[int]:
    return [r.id for r in getattr(interaction.user, "roles", [])]


def modal_values(data: dict) -> dict[str, str]:
    """从 Modal 提交的原始数据里取出所有输入框的值（兼容 ActionRow / Label 两种结构）。"""
    out: dict[str, str] = {}

    def walk(node):
        if isinstance(node, dict):
            if "custom_id" in node and "value" in node:
                out[node["custom_id"]] = node["value"]
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk((data or {}).get("components", []))
    return out


class TextModal(discord.ui.Modal):
    """正文输入框。提交由 PersonaProxy.on_interaction 按 custom_id 处理，这里什么也不做。"""

    def __init__(self, custom_id: str, title: str, max_length: int):
        super().__init__(title=title, timeout=None, custom_id=custom_id)
        self.add_item(discord.ui.TextInput(label="内容", style=discord.TextStyle.paragraph, custom_id="text",
                                           max_length=max_length, required=True))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        return None


class ProfileModal(discord.ui.Modal):
    def __init__(self, cog: PersonaProxy, mode: str, profile: dict | None = None):
        cfg = cog.service.cfg
        super().__init__(title="创建皮套" if mode == "create" else "编辑皮套", timeout=None)
        self.cog, self.mode, self.profile_id = cog, mode, profile["id"] if profile else None
        p = profile or {}
        self.name = discord.ui.TextInput(label="角色名称", max_length=cfg["name_max_length"], default=p.get("name"))
        self.avatar = discord.ui.TextInput(label="头像 URL（http/https）", max_length=cfg["avatar_url_max_length"],
                                           default=p.get("avatar_url"))
        self.desc = discord.ui.TextInput(label="人设描述（可空，/send 时原样发出）", style=discord.TextStyle.paragraph,
                                         required=False, max_length=cfg["description_max_length"],
                                         default=p.get("description") or None)
        for item in (self.name, self.avatar, self.desc):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_profile(interaction, self.mode, self.profile_id,
                                      self.name.value, self.avatar.value, self.desc.value)


class ConfirmDelete(discord.ui.View):
    def __init__(self, cog: PersonaProxy, owner_id: int, profile: dict):
        super().__init__(timeout=120)
        self.cog, self.owner_id, self.profile = cog, owner_id, profile

    @discord.ui.button(label="确认删除", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("这不是你的操作。", ephemeral=True)
        try:
            await self.cog.service.delete_profile(interaction.guild_id, self.owner_id, self.profile["id"])
            text = f"🗑️ 已删除皮套「{discord.utils.escape_markdown(self.profile['name'])}」，配额已释放。历史消息不受影响。"
        except PersonaError as e:
            text = str(e)
        except StorageError:
            text = MSG_STORAGE
        self.stop()
        await interaction.response.edit_message(content=text, view=None)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="已取消。", view=None)


class ProfilePicker(discord.ui.View):
    """「以皮套回复」：仅本人可见的皮套选择面板，超过 25 个时分页。"""

    def __init__(self, cog: PersonaProxy, owner_id: int, profiles: list[dict], target: discord.Message, page: int = 0):
        super().__init__(timeout=600)
        self.cog, self.owner_id, self.profiles, self.target, self.page = cog, owner_id, profiles, target, page
        pages = max(1, (len(profiles) + PAGE - 1) // PAGE)
        chunk = profiles[page * PAGE:(page + 1) * PAGE]
        select = discord.ui.Select(placeholder=f"选择皮套（{page + 1}/{pages}）",
                                   options=[discord.SelectOption(label=ui.profile_label(p), value=p["id"]) for p in chunk])
        select.callback = self.on_select
        self.add_item(select)
        if pages > 1:
            prev = discord.ui.Button(label="上一页", disabled=page == 0)
            nxt = discord.ui.Button(label="下一页", disabled=page >= pages - 1)
            prev.callback = lambda i: self.turn(i, -1)
            nxt.callback = lambda i: self.turn(i, 1)
            self.add_item(prev)
            self.add_item(nxt)

    async def turn(self, interaction: discord.Interaction, delta: int):
        await interaction.response.edit_message(
            view=ProfilePicker(self.cog, self.owner_id, self.profiles, self.target, self.page + delta))

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("这不是你的面板。", ephemeral=True)
        pid = interaction.data["values"][0]
        try:
            self.cog.service.require_speaker(interaction.guild_id, interaction.user.id, role_ids(interaction), pid)
            check_channel(interaction)
        except PersonaError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        except StorageError:
            return await interaction.response.send_message(MSG_STORAGE, ephemeral=True)
        # 用 Select 的这个新 interaction 打开 Modal（不能在已响应的 interaction 上再 send_modal）
        modal = TextModal(f"pp:reply:{pid}:{self.target.id}:{secrets.token_hex(6)}", "以皮套回复",
                          self.cog.service.cfg["reply_text_max_length"])
        await interaction.response.send_modal(modal)


class PersonaProxy(commands.Cog, name="PersonaProxy"):
    profile = app_commands.Group(name="profile", description="管理你的皮套", guild_only=True)
    grant_group = app_commands.Group(name="grant", description="（管理）皮套配额", guild_only=True,
                                     default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: commands.Bot, service: PersonaService | None = None, sender: WebhookSender | None = None):
        self.bot = bot
        self.service = service or PersonaService()
        KNOWN_WEBHOOK_IDS.update(self.service.store.all_webhook_ids())
        self.sender = sender or WebhookSender(bot, self.service.cfg["webhook_name"],
                                              on_new_webhook=self.service.remember_webhook)
        self.reply_menu = app_commands.ContextMenu(name="以皮套回复", callback=self.reply_context)
        self.reply_menu.guild_only = True
        self.audit_menu = app_commands.ContextMenu(name="皮套查询（管理）", callback=self.audit_context)
        self.audit_menu.guild_only = True
        self.audit_menu.default_permissions = discord.Permissions(manage_guild=True)

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.reply_menu)
        self.bot.tree.add_command(self.audit_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.reply_menu.name, type=self.reply_menu.type)
        self.bot.tree.remove_command(self.audit_menu.name, type=self.audit_menu.type)

    # ------------------------------------------------------------------ 通用

    @property
    def cfg(self) -> dict:
        return self.service.cfg

    @staticmethod
    async def err(interaction: discord.Interaction, text: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.send_message(text, ephemeral=True,
                                                        allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("皮套：回复失败: %s", e)

    def _profile_choices(self, interaction: discord.Interaction, current: str, usable_only: bool):
        try:
            doc = self.service.store.load(interaction.guild_id)
        except StorageError:
            return []
        uid, roles = interaction.user.id, role_ids(interaction)
        usable = {p["id"] for p in self.service.usable(doc, uid, roles)}
        rows = self.service.usable(doc, uid, roles) if usable_only else self.service.owned(doc, uid)
        cur = current.casefold()
        choices = [app_commands.Choice(name=ui.profile_label(p, p["id"] not in usable), value=p["id"])
                   for p in rows if cur in ui.profile_label(p).casefold()]
        return choices[:25]

    async def ac_usable(self, interaction: discord.Interaction, current: str):
        return self._profile_choices(interaction, current, usable_only=True)

    async def ac_own(self, interaction: discord.Interaction, current: str):
        return self._profile_choices(interaction, current, usable_only=False)

    async def ac_drafts(self, interaction: discord.Interaction, current: str):
        try:
            doc = self.service.store.load(interaction.guild_id)
            drafts = self.service.retryable(interaction.guild_id, interaction.user.id)
        except StorageError:
            return []
        names = {p["id"]: p["name"] for p in doc["profiles"]}
        cur = current.casefold()
        out = []
        for d in drafts:
            label = ui.draft_label(d, names.get(d["profile_id"], "已删除的皮套"))
            if cur in label.casefold():
                out.append(app_commands.Choice(name=label, value=d["id"]))
        return out[:25]

    # ------------------------------------------------------------------ /profile

    @profile.command(name="create", description="创建一个新皮套")
    async def profile_create(self, interaction: discord.Interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        try:
            if self.service.guild_cfg(gid) is None:
                raise PersonaError("本服没有开启皮套功能。")
            if not self.service.has_role(gid, role_ids(interaction)):
                raise PersonaError("你没有使用皮套的身份组。")
            doc = self.service.store.load(gid)
            limit, count = self.service.limit_of(doc, uid, role_ids(interaction)), len(self.service.owned(doc, uid))
            if limit <= 0 or count >= limit:
                raise PersonaError(f"你已经有 {count} 个皮套，配额上限是 {limit}。")
        except PersonaError as e:
            return await self.err(interaction, str(e))
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        await interaction.response.send_modal(ProfileModal(self, "create"))

    @profile.command(name="edit", description="编辑你的皮套")
    @app_commands.autocomplete(profile=ac_own)
    async def profile_edit(self, interaction: discord.Interaction, profile: str):
        try:
            p = self.service.get_own(interaction.guild_id, interaction.user.id, profile)
        except PersonaError as e:
            return await self.err(interaction, str(e))
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        await interaction.response.send_modal(ProfileModal(self, "edit", p))

    @profile.command(name="list", description="查看你的皮套、启用状态与配额")
    async def profile_list(self, interaction: discord.Interaction):
        try:
            doc = self.service.store.load(interaction.guild_id)
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        uid, roles = interaction.user.id, role_ids(interaction)
        embed = ui.profile_list_embed(self.service.owned(doc, uid),
                                      {p["id"] for p in self.service.usable(doc, uid, roles)},
                                      self.service.limit_of(doc, uid, roles),
                                      self.service.has_role(interaction.guild_id, role_ids(interaction)))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @profile.command(name="delete", description="删除你的一个皮套（释放配额，不删除历史消息）")
    @app_commands.autocomplete(profile=ac_own)
    async def profile_delete(self, interaction: discord.Interaction, profile: str):
        try:
            p = self.service.get_own(interaction.guild_id, interaction.user.id, profile)
        except PersonaError as e:
            return await self.err(interaction, str(e))
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        await interaction.response.send_message(
            f"确定要删除皮套「{discord.utils.escape_markdown(p['name'])}」（`{p['id']}`）吗？", ephemeral=True,
            view=ConfirmDelete(self, interaction.user.id, p))

    async def submit_profile(self, interaction: discord.Interaction, mode: str, profile_id: str | None,
                             name: str, avatar: str, desc: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        gid, uid = interaction.guild_id, interaction.user.id
        try:
            if mode == "create":
                p, count, limit = await self.service.create_profile(gid, uid, role_ids(interaction), name, avatar, desc)
            else:
                p = await self.service.edit_profile(gid, uid, profile_id, name, avatar, desc)
        except PersonaError as e:
            return await self.err(interaction, str(e))
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        verb = "创建" if mode == "create" else "更新"
        await self.err(interaction, f"✅ 已{verb}皮套「{discord.utils.escape_markdown(p['name'])}」（`{p['id']}`）。")
        if mode == "create":
            await self.post_creation_log(gid, p, uid, count, limit)

    async def post_creation_log(self, guild_id: int, p: dict, owner_id: int, count: int, limit: int) -> None:
        """仅在 Profile 已持久化后调用；失败只记录，不回滚 Profile，也不发到其他频道。"""
        tid = (self.service.guild_cfg(guild_id) or {}).get("creation_log_thread_id")
        status, mid = "failed", None
        try:
            if not tid:
                log.error("皮套：guild=%s 未配置 creation_log_thread_id，创建日志缺失 profile=%s", guild_id, p["id"])
                status = "not_configured"
                return
            thread = self.bot.get_channel(int(tid))
            if thread is None:
                thread = await self.bot.fetch_channel(int(tid))
            if not isinstance(thread, discord.Thread) or thread.guild.id != guild_id:
                log.error("皮套：guild=%s 的创建日志 thread 配置无效", guild_id)
                return
            msg = await thread.send(embed=ui.creation_log_embed(p, owner_id, count, limit),
                                    allowed_mentions=discord.AllowedMentions.none())
            status, mid = "sent", msg.id
        except discord.HTTPException as e:
            log.error("皮套：guild=%s 创建日志发送失败（%s）profile=%s", guild_id, e.status, p["id"])
        except asyncio.TimeoutError:
            status = "unknown"   # 不确定是否发出：不自动重发
            log.error("皮套：guild=%s 创建日志发送结果不明 profile=%s", guild_id, p["id"])
        finally:
            try:
                await self.service.mark_creation_log(guild_id, p["id"], status, mid)
            except StorageError:
                pass

    # ------------------------------------------------------------------ /grant profile

    @grant_group.command(name="profile", description="（管理）设置某人的皮套数量上限（覆盖旧值）")
    @app_commands.describe(user="玩家", limit="新的上限（0 = 全部停用但保留数据）")
    async def grant_profile(self, interaction: discord.Interaction, user: discord.User,
                            limit: app_commands.Range[int, 0, 50]):
        perms = interaction.permissions
        if not (perms.manage_guild or perms.administrator):
            return await self.err(interaction, "只有拥有「管理服务器」权限的管理员可以调整配额。")
        try:
            r = await self.service.grant(interaction.guild_id, user.id, limit,
                                         [x.id for x in getattr(user, "roles", [])])
        except PersonaError as e:
            return await self.err(interaction, str(e))
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        note = ("\n⚠️ 对方持有不受配额限制的身份组，这个设置暂时不生效；身份组被撤后才会按它计算。"
                if r["unlimited"] else "")
        await self.err(interaction, f"<@{user.id}> 的皮套上限：{r['old']} → **{r['new']}**；"
                                    f"现有 {r['count']} 个，当前可用 {r['usable']} 个。{note}")

    # ------------------------------------------------------------------ /say /send 重试

    @app_commands.command(name="say", description="用皮套发言；不填内容会打开长文输入框")
    @app_commands.describe(profile="皮套", message="内容（不填则打开输入框，适合长文）")
    @app_commands.autocomplete(profile=ac_usable)
    @app_commands.guild_only()
    async def say(self, interaction: discord.Interaction, profile: str,
                  message: app_commands.Range[str, 1, 2000] | None = None):
        gid, uid = interaction.guild_id, interaction.user.id
        try:
            self.service.require_speaker(gid, uid, role_ids(interaction), profile)
            check_channel(interaction)
        except PersonaError as e:
            return await self.err(interaction, str(e))
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        if message is None:
            # 直接用初始 interaction 打开 Modal（不能先 defer）
            modal = TextModal(f"pp:say:{profile}:{secrets.token_hex(6)}", "皮套发言", self.cfg["message_max_length"])
            return await interaction.response.send_modal(modal)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            draft = await self.service.save_draft(gid, uid, profile, interaction.channel_id, "say", message)
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        await self.deliver(interaction, draft)

    @app_commands.command(name="send", description="在展示频道用皮套发出人设描述和头像")
    @app_commands.autocomplete(profile=ac_usable)
    @app_commands.guild_only()
    async def send_showcase(self, interaction: discord.Interaction, profile: str):
        gid, uid = interaction.guild_id, interaction.user.id
        try:
            self._check_display_channel(interaction)
            p = self.service.require_speaker(gid, uid, role_ids(interaction), profile)
            check_channel(interaction)
            if not p["description"].strip():
                raise PersonaError("这个皮套没有人设描述，先用 /profile edit 写一段吧。")
        except PersonaError as e:
            return await self.err(interaction, str(e))
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            draft = await self.service.save_draft(gid, uid, profile, interaction.channel_id, "showcase",
                                                  p["description"], extra={"avatar_url": p["avatar_url"]})
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        await self.deliver(interaction, draft)

    def _check_display_channel(self, interaction: discord.Interaction) -> None:
        g = self.service.guild_cfg(interaction.guild_id)
        if g is None:
            raise PersonaError("本服没有开启皮套功能。")
        display = g.get("profile_display_channel_id")
        if not display:
            raise PersonaError("本服还没有配置皮套展示频道。")
        if interaction.channel_id != int(display):
            raise PersonaError(f"/send 只能在 <#{display}> 使用。")

    @app_commands.command(name="say-retry", description="重发你保存下来的皮套草稿（需在原频道使用）")
    @app_commands.autocomplete(draft=ac_drafts)
    @app_commands.guild_only()
    async def say_retry(self, interaction: discord.Interaction, draft: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            d = next((x for x in self.service.retryable(interaction.guild_id, interaction.user.id) if x["id"] == draft),
                     None)
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        if d is None:
            return await self.err(interaction, "找不到这份草稿，或它已经发出了。")
        await self.deliver(interaction, d)

    @app_commands.command(name="say-draft", description="查看你保存下来的皮套草稿原文")
    @app_commands.autocomplete(draft=ac_drafts)
    @app_commands.guild_only()
    async def say_draft(self, interaction: discord.Interaction, draft: str):
        try:
            doc = self.service.store.load(interaction.guild_id)
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        d = doc["drafts"].get(draft)
        if d is None or d["owner_id"] != str(interaction.user.id):
            return await self.err(interaction, "找不到这份草稿。")
        name = next((p["name"] for p in doc["profiles"] if p["id"] == d["profile_id"]), "已删除的皮套")
        await interaction.response.send_message(embed=ui.draft_embed(d, name), ephemeral=True)

    # ------------------------------------------------------------------ Modal 提交（重启后也能接住）

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.modal_submit:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        parts = cid.split(":")
        if len(parts) < 4 or parts[0] != "pp" or parts[1] not in ("say", "reply"):
            return
        if interaction.guild_id is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = modal_values(interaction.data).get("text", "")
        kind, pid = parts[1], parts[2]
        reply_to = None
        if kind == "reply":
            if len(parts) != 5 or not parts[3].isdigit():
                return await self.err(interaction, "这个输入框已经失效了。")
            mid = int(parts[3])
            reply_to = {"message_id": str(mid), "channel_id": str(interaction.channel_id),
                        "jump_url": f"https://discord.com/channels/{interaction.guild_id}/{interaction.channel_id}/{mid}"}
        try:
            # 第一件事：把收到的正文落盘（同一个 Modal 重复提交只保存一次）
            draft = await self.service.save_draft(interaction.guild_id, interaction.user.id, pid,
                                                  interaction.channel_id, kind, text, reply_to=reply_to,
                                                  draft_id=f"d_{parts[-1]}")
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        await self.deliver(interaction, draft)

    # ------------------------------------------------------------------ 发送

    async def deliver(self, interaction: discord.Interaction, draft: dict) -> None:
        """认领草稿 → 全量重验 → 发送 → 记录。任何失败都保留草稿。"""
        gid, uid = interaction.guild_id, interaction.user.id
        try:
            d = await self.service.claim(gid, uid, draft["id"])
        except PersonaError as e:
            return await self.err(interaction, str(e))
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        hint = DRAFT_HINT.format(id=d["id"])
        fail_status = PARTIAL if d["sent_parts"] else FAILED
        sent_ids: list[int] = []
        try:
            if str(interaction.channel_id) != d["channel_id"]:
                raise PersonaError(f"请到草稿原来的频道 <#{d['channel_id']}> 重试。")
            profile = self.service.require_speaker(gid, uid, role_ids(interaction), d["profile_id"])
            check_channel(interaction)
            channel = interaction.channel
            if d["kind"] == "showcase":
                self._check_display_channel(interaction)
            elif d["kind"] == "reply":
                try:
                    await channel.fetch_message(int(d["reply_to"]["message_id"]))
                except discord.HTTPException:
                    raise PersonaError("原消息已经不可访问了，不能回复。")
                content = self.service.validate_message(ui.reply_content(d["reply_to"]["jump_url"], d["text"]))
            else:
                content = self.service.validate_message(d["text"])

            if d["kind"] == "showcase":
                async with self.sender.channel_lock(channel.id):   # 两条之间尽量不被插队
                    if d["sent_parts"] == 0:
                        first = await self.sender.send(channel, profile, d["text"])
                        sent_ids.append(first)
                        d = await self.service.finish(gid, d["id"], SENDING, [first], sent_parts=1)
                        fail_status = PARTIAL
                    second = await self.sender.send(channel, profile, d["extra"]["avatar_url"])
                    sent_ids.append(second)
                    d = await self.service.finish(gid, d["id"], SENT, [second], sent_parts=2, profile_id=profile["id"])
            else:
                mid = await self.sender.send(channel, profile, content)
                sent_ids.append(mid)
                d = await self.service.finish(gid, d["id"], SENT, [mid], profile_id=profile["id"])
        except PersonaError as e:
            await self._safe_finish(gid, d["id"], fail_status, str(e))
            return await self.err(interaction, f"{e}{hint}")
        except SendFailed as e:
            await self._safe_finish(gid, d["id"], fail_status, str(e))
            return await self.err(interaction, f"❌ 发送失败：{e}{hint}")
        except SendUnknown as e:
            await self._safe_finish(gid, d["id"], UNKNOWN, str(e))
            return await self.err(interaction, f"⚠️ {e}请先看看频道里有没有发出，不要重复发送。"
                                               f"\n原文仍保存在草稿 `{d['id']}`，可用 `/say-draft` 查看。")
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)

        self.service.audit(gid, {"draft_id": d["id"], "kind": d["kind"], "owner_id": str(uid),
                                 "profile_id": profile["id"], "profile_name": profile["name"],
                                 "channel_id": d["channel_id"], "message_ids": d["message_ids"],
                                 "reply_to": d["reply_to"]})
        await self.err(interaction, f"✅ 已用「{discord.utils.escape_markdown(profile['name'])}」发出。")

    async def _safe_finish(self, gid: int, draft_id: str, status: str, error: str) -> None:
        try:
            await self.service.finish(gid, draft_id, status, error=error)
        except StorageError:
            log.error("皮套：guild=%s 草稿 %s 状态更新失败", gid, draft_id)

    # ------------------------------------------------------------------ 右键菜单

    async def reply_context(self, interaction: discord.Interaction, message: discord.Message):
        gid, uid = interaction.guild_id, interaction.user.id
        try:
            if self.service.guild_cfg(gid) is None:
                raise PersonaError("本服没有开启皮套功能。")
            if not self.service.has_role(gid, role_ids(interaction)):
                raise PersonaError("你没有使用皮套的身份组。")
            check_channel(interaction)
            usable = self.service.usable(self.service.store.load(gid), uid, role_ids(interaction))
            if not usable:
                raise PersonaError("你没有可用的皮套。")
        except PersonaError as e:
            return await self.err(interaction, str(e))
        except StorageError:
            return await self.err(interaction, MSG_STORAGE)
        await interaction.response.send_message("选择用哪个皮套回复这条消息：", ephemeral=True,
                                                view=ProfilePicker(self, uid, usable, message))

    async def audit_context(self, interaction: discord.Interaction, message: discord.Message):
        perms = interaction.permissions
        if not (perms.manage_guild or perms.administrator):
            return await self.err(interaction, "只有管理员可以查询。")
        rec = self.service.store.find_audit(interaction.guild_id, message.id)
        if rec is None:
            return await self.err(interaction, "这条消息不是玉猫皮套发出的，或没有审计记录。")
        await interaction.response.send_message(ui.audit_text(rec), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

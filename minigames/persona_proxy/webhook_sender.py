"""皮套系统：Webhook 获取与代发、频道权限检查。

- 每个父频道复用一个玉猫自己创建的 Webhook（子区通过 thread= 发送），发送时覆盖名字和头像；
- Webhook 被删（404）时重新获取一次，避免无限创建；token 只在内存，不进日志、不落盘；
- Discord 明确返回错误 = SendFailed（确定没发出）；网络超时等 = SendUnknown（不确定，禁止自动重试）。
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
import discord

from .service import PersonaError

log = logging.getLogger(__name__)

# 玉猫皮套 Webhook 的 id；bot.py 用它忽略皮套消息，避免触发聊天关键词等无关功能
KNOWN_WEBHOOK_IDS: set[int] = set()


def is_persona_webhook(webhook_id: int | None) -> bool:
    return webhook_id is not None and int(webhook_id) in KNOWN_WEBHOOK_IDS


class SendFailed(Exception):
    """Discord 明确拒绝，消息确定没有发出。"""


class SendUnknown(Exception):
    """请求结果不明（超时/断线），消息可能已经发出。"""


SUPPORTED_PARENTS = (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel)


def check_channel(interaction: discord.Interaction) -> None:
    """操作者必须能在这里正常发言；玉猫必须能管理 Webhook。禁止借 Webhook 越过只读权限。"""
    ch = interaction.channel
    is_thread = isinstance(ch, discord.Thread)
    if not (is_thread or isinstance(ch, (discord.TextChannel, discord.VoiceChannel))):
        raise PersonaError("这个频道不支持皮套发言。")
    perms = interaction.permissions
    if not perms.view_channel:
        raise PersonaError("你看不到这个频道。")
    if is_thread:
        if not perms.send_messages_in_threads:
            raise PersonaError("你在这个子区没有发言权限。")
        if getattr(ch, "locked", False) and not perms.manage_threads:
            raise PersonaError("这个子区已锁定。")
    elif not perms.send_messages:
        raise PersonaError("你在这个频道没有发言权限。")
    if not interaction.app_permissions.manage_webhooks:
        raise PersonaError("玉猫在这个频道缺少「管理 Webhook」权限，请联系管理员。")


class WebhookSender:
    def __init__(self, bot: discord.Client, webhook_name: str, on_new_webhook=None):
        self.bot = bot
        self.webhook_name = webhook_name
        self.on_new_webhook = on_new_webhook          # async (guild_id, channel_id, webhook_id)
        self.cache: dict[int, discord.Webhook] = {}
        self.locks: dict[int, asyncio.Lock] = {}      # 每个父频道一把：避免并发创建，也保证 /send 两条连续

    def channel_lock(self, parent_id: int) -> asyncio.Lock:
        return self.locks.setdefault(parent_id, asyncio.Lock())

    async def resolve(self, channel) -> tuple[discord.abc.GuildChannel, discord.Thread | None]:
        if isinstance(channel, discord.Thread):
            parent = channel.parent or self.bot.get_channel(channel.parent_id)
            if parent is None:
                parent = await self.bot.fetch_channel(channel.parent_id)
            if not isinstance(parent, SUPPORTED_PARENTS):
                raise PersonaError("这个子区不支持皮套发言。")
            return parent, channel
        return channel, None

    async def _webhook(self, parent, fresh: bool = False) -> discord.Webhook:
        if not fresh and parent.id in self.cache:
            return self.cache[parent.id]
        me = self.bot.user.id
        found = None
        for wh in await parent.webhooks():
            if wh.user and wh.user.id == me and wh.name == self.webhook_name and wh.token:
                found = wh
                break
        if found is None:
            found = await parent.create_webhook(name=self.webhook_name, reason="玉猫皮套代发")
        self.cache[parent.id] = found
        if found.id not in KNOWN_WEBHOOK_IDS:
            KNOWN_WEBHOOK_IDS.add(found.id)
            if self.on_new_webhook:
                await self.on_new_webhook(parent.guild.id, parent.id, found.id)
        return found

    async def send(self, channel, profile: dict, content: str, mention_user_id: int | None = None) -> int:
        """以皮套身份发送一条消息，返回消息 id。

        默认不提醒任何人；mention_user_id 只用于「回复」时提醒被回复者本人，正文里的其他 @ 仍然不提醒。
        """
        allowed = (discord.AllowedMentions(everyone=False, roles=False, replied_user=False,
                                           users=[discord.Object(mention_user_id)])
                   if mention_user_id else discord.AllowedMentions.none())
        parent, thread = await self.resolve(channel)
        for attempt in (0, 1):
            try:
                wh = await self._webhook(parent, fresh=attempt == 1)
            except discord.Forbidden:
                raise SendFailed("玉猫没有权限管理这个频道的 Webhook。")
            except discord.HTTPException as e:
                raise SendFailed(f"获取 Webhook 失败（{e.status}）。")
            kwargs = dict(username=profile["name"], avatar_url=profile["avatar_url"],
                          allowed_mentions=allowed, wait=True)
            if thread is not None:
                kwargs["thread"] = thread
            try:
                msg = await wh.send(content, **kwargs)
                return msg.id
            except discord.NotFound:
                # Webhook 被删：确定没发出，重新获取一次
                self.cache.pop(parent.id, None)
                if attempt == 1:
                    raise SendFailed("Webhook 不可用。")
            except discord.HTTPException as e:
                raise SendFailed(f"Discord 拒绝了这条消息（{e.status}）。")
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                log.warning("皮套：webhook 发送结果不明 channel=%s: %s", channel.id, type(e).__name__)
                raise SendUnknown("网络异常，无法确认是否已发出。")
        raise SendFailed("Webhook 不可用。")

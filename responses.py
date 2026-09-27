import discord
import asyncio
import config
import random
from datetime import datetime

# 中文对话响应配置
CHAT_CATEGORY_ID = 1352481256792789093  # 中文对话频道类别ID
CHAT_RESPONSES = {
    "你好玉猫": [
        "本喵不好，喵呜~",
        "喂喵花生，喵~",
        "你抚琵琶奏琴弦~",
        "喵~ 今天心情不错",
        "玉猫在此，有何吩咐？",
        "别叫本喵，喵想静静"
    ],
    "摸摸玉猫": [
        "喵呜~",
        "脏手拿开喵~",
        "那我问你！本喵有几根毛！",
        "喵~ 方向错了！！"
    ],
    "玉猫在吗": [
        "在呢在呢~",
        "不在，别找喵~",
        "喵喵喵？",
        "忙着抓玉鼠呢，等会再说"
    ],
    "谢谢玉猫": [
        "不客气喵~",
        "记得喂本喵小鱼干哦~",
        "下次再来找喵玩呀",
        "能帮到你就好"
    ],
    "玉猫真棒": [
        "那当然，本喵可是最聪明的猫，喵~",
        "低调低调~",
        "谢谢夸奖，喵会继续努力的",
        "喵哈哈，开心"
    ],
    "玉猫唱歌": [
        "哈基米哦南北绿豆，阿西嘎哈椰果奶龙🎶",
        "哈基米！南北绿豆!阿西嘎哈!亚库奶龙🎶",
        "哈基米莫南北绿豆，阿西哈呀酷奶龙🎶",
        "哈基米基米基，阿西嘎嘎阿西，曼波曼波曼曼波，叮咚咚叮，叮叮叮咚咚叮🎶",
        "哈基米南北绿豆曼波！悠达悠达曼波!哦马吉利！哈呀酷奶龙🎶"
    ]
}

# 人设搜索频道ID列表（注意：特殊频道已独立处理，此处保留但不影响）
CHARACTER_SEARCH_CHANNELS = [1362807302088036524, 1238157595001491549]

# 特殊人设搜索频道（独立处理，只搜索其映射的两个子区）
SPECIAL_CHAR_SEARCH_CHANNEL = 1238157595001491549

# location 功能配置
LOCATION_SERVER_ID = 1032497061318365204  # 指定服务器ID
LOCATION_CATEGORY_ID = 1040202548168114216  # 指定类别ID
LOCATION_TARGET_CATEGORY_IDS = [  # 需要搜索的7个类别ID
    1041278677561917460,
    1087232391103381534,
    1039833969249759262,
    1177905711473250345,
    1039834194672631838,
    1062652000057167942,
    1043864374248493086
]

# 缓存最近搜索的用户消息位置
user_location_cache = {}
CACHE_DURATION = 300  # 缓存5分钟（300秒）

# 笨蛋特例配置
IDIOT_TRIGGER_CHANNEL_ID = 1391776324749627443
IDIOT_RESPONSE = "✅ 找到 1/300 条结果:\n<@1077820828063899752> 在 <#1282654057197993997>: https://discord.com/channels/1282636155468120166/1282654057197993997/1397551543137337426"


async def send_online_notice(bot):
    """发送上线通知"""
    notice_channel = bot.get_channel(config.ONLINE_NOTICE_CHANNEL_ID)
    if notice_channel:
        try:
            await notice_channel.send(
                "会找人的猫猫出现喽！")
            print(f"已发送上线通知到频道: {config.ONLINE_NOTICE_CHANNEL_ID}")
        except Exception as e:
            print(f"发送上线通知失败: {e}")
    else:
        print(f"警告: 找不到上线通知频道 (ID: {config.ONLINE_NOTICE_CHANNEL_ID})")


async def handle_category_chat(bot, message):
    """处理特定类别中的中文对话"""
    # 检查是否在指定的频道类别中
    if message.channel.category_id != CHAT_CATEGORY_ID:
        return False

    # 检查消息内容是否匹配预设的关键词
    content = message.content.strip()
    for keyword, responses in CHAT_RESPONSES.items():
        if keyword in content:
            # 随机选择一个回复
            reply = random.choice(responses)
            await message.channel.send(reply)
            return True

    return False


async def handle_normal_search(bot, message, search_username):
    """处理普通用户名搜索"""
    control_channel_id = message.channel.id
    search_channel_ids = config.CONTROL_TO_SEARCH_MAP.get(control_channel_id, [])

    if not search_channel_ids:
        temp_msg = await message.reply("⚠️ 错误：未配置该控制频道对应的搜索频道")
        await asyncio.sleep(10)
        try:
            await temp_msg.delete()
        except:
            pass
        return

    # 显示搜索信息
    search_msg = await message.channel.send(
        f"🔍 正在搜索用户名: `{search_username}`...\n"
        f"📂 搜索频道: {', '.join([f'<#{cid}>' for cid in search_channel_ids])}"
    )

    found_messages = []
    search_count = 0
    search_lower = search_username.lower()

    for channel_id in search_channel_ids:
        channel = bot.get_channel(channel_id)
        if not channel:
            print(f"频道不存在: {channel_id}")
            continue

        try:
            async for msg in channel.history(limit=500):
                search_count += 1
                author = msg.author
                names_to_check = [author.name]

                if isinstance(author, discord.Member) and author.nick:
                    names_to_check.append(author.nick)

                if hasattr(author, 'global_name') and author.global_name:
                    names_to_check.append(author.global_name)

                # 检查用户名是否包含搜索字符串（不区分大小写）
                if any(search_lower in name.lower() for name in names_to_check):
                    message_link = f"https://discord.com/channels/{msg.guild.id}/{msg.channel.id}/{msg.id}"
                    found_messages.append(f"{author.mention} 在 {channel.mention}: {message_link}")

                    if len(found_messages) >= 5:
                        break
        except discord.Forbidden:
            print(f"无法访问频道: {channel_id}")
            temp_err = await message.channel.send(f"⚠️ 无权限访问频道: {channel.name}")
            await asyncio.sleep(10)
            try:
                await temp_err.delete()
            except:
                pass

    try:
        await search_msg.delete()
    except:
        pass

    if found_messages:
        result = "\n".join(found_messages)
        await message.reply(f"✅ 找到 {len(found_messages)}/{search_count} 条结果:\n{result}")
    else:
        await message.reply(f"❌ 未找到用户名 `{search_username}` 的消息")


async def search_mentioned_messages(bot, channel, target_user_id):
    """在频道及其子区中搜索提及目标用户的消息"""
    earliest_mentioned_message = None

    # 搜索频道本身的消息
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            # 检查消息是否提及目标用户
            if any(user.id == target_user_id for user in msg.mentions):
                earliest_mentioned_message = msg
                break  # 找到最早的一条就停止
    except discord.Forbidden:
        print(f"无法访问频道: {channel.id}")
        return None
    except Exception as e:
        print(f"搜索频道 {channel.id} 时出错: {e}")
        return None

    # 如果已经在频道中找到消息，直接返回
    if earliest_mentioned_message:
        return earliest_mentioned_message

    # 搜索频道的所有子区
    try:
        # 获取活跃的子区
        threads = channel.threads
        # 获取归档的子区（需要权限）
        archived_threads = []
        try:
            async for thread in channel.archived_threads(limit=None):
                archived_threads.append(thread)
        except discord.Forbidden:
            print(f"无法获取频道 {channel.id} 的归档子区")

        all_threads = threads + archived_threads

        for thread in all_threads:
            try:
                async for msg in thread.history(limit=None, oldest_first=True):
                    # 检查消息是否提及目标用户
                    if any(user.id == target_user_id for user in msg.mentions):
                        return msg  # 找到最早的一条就停止
            except discord.Forbidden:
                print(f"无法访问子区: {thread.id}")
                continue
            except Exception as e:
                print(f"搜索子区 {thread.id} 时出错: {e}")
                continue
    except Exception as e:
        print(f"获取频道 {channel.id} 的子区时出错: {e}")

    return None


async def handle_character_search(bot, message, target_user):
    """处理人设搜索 - 找到提及目标用户的最早消息（通用版本）"""
    control_channel_id = message.channel.id
    search_channel_ids = config.CONTROL_TO_SEARCH_MAP.get(control_channel_id, [])

    if not search_channel_ids:
        temp_msg = await message.reply("⚠️ 错误：未配置该控制频道对应的搜索频道")
        await asyncio.sleep(10)
        try:
            await temp_msg.delete()
        except:
            pass
        return

    # 显示搜索信息
    search_msg = await message.channel.send(
        f"🔍 正在搜索提及用户 {target_user.mention} 的最早聊天记录...\n"
        f"📂 搜索范围: 对应搜索频道及其所有子区\n"
        f"📋 搜索频道: {', '.join([f'<#{cid}>' for cid in search_channel_ids])}"
    )

    earliest_message = None

    # 同时搜索所有频道，找到最早的消息
    search_tasks = []
    for channel_id in search_channel_ids:
        channel = bot.get_channel(channel_id)
        if channel:
            search_tasks.append(search_mentioned_messages(bot, channel, target_user.id))

    # 等待所有搜索任务完成
    results = await asyncio.gather(*search_tasks, return_exceptions=True)

    # 过滤有效结果并找到最早的消息
    for result in results:
        if isinstance(result, Exception) or result is None:
            continue

        if earliest_message is None or result.created_at < earliest_message.created_at:
            earliest_message = result

    try:
        await search_msg.delete()
    except:
        pass

    if earliest_message:
        message_link = f"https://discord.com/channels/{earliest_message.guild.id}/{earliest_message.channel.id}/{earliest_message.id}"
        await message.reply(f"哈吉米 {target_user.mention} 南北路多 {message_link}")
    else:
        await message.reply(f"❌ 未找到提及用户 {target_user.mention} 的聊天记录")


async def get_channel_with_threads(channel):
    """获取频道及其所有子区"""
    threads = []

    # 获取活跃的子区
    if hasattr(channel, 'threads'):
        threads.extend(channel.threads)

    # 尝试获取归档的子区
    try:
        if hasattr(channel, 'archived_threads'):
            async for thread in channel.archived_threads(limit=50):  # 限制归档子区数量
                threads.append(thread)
    except:
        pass

    return threads


async def quick_search_user_in_channel(channel, target_user_id):
    """快速搜索用户在频道中的最新消息"""
    try:
        # 方法1: 从最新消息开始搜索（最近100条）
        async for msg in channel.history(limit=100, oldest_first=False):
            if msg.author.id == target_user_id:
                return msg

        # 方法2: 检查子区
        threads = await get_channel_with_threads(channel)

        for thread in threads:
            try:
                async for msg in thread.history(limit=50, oldest_first=False):
                    if msg.author.id == target_user_id:
                        return msg
            except:
                continue

        # 方法3: 如果上面没找到，尝试搜索更早的消息（再搜100条）
        async for msg in channel.history(limit=100, before=None, after=None, oldest_first=False):
            if msg.author.id == target_user_id:
                return msg

    except discord.Forbidden:
        return None
    except Exception as e:
        print(f"搜索频道 {channel.id} 时出错: {e}")

    return None


async def parallel_search_category(guild, category_id, target_user_id):
    """并行搜索一个类别下的所有频道"""
    category = guild.get_channel(category_id)
    if not category or not isinstance(category, discord.CategoryChannel):
        return None

    # 收集所有搜索任务
    search_tasks = []

    for channel in category.channels:
        if isinstance(channel, discord.TextChannel):
            search_tasks.append(quick_search_user_in_channel(channel, target_user_id))

    # 并行执行所有搜索任务
    results = await asyncio.gather(*search_tasks, return_exceptions=True)

    # 找到最新的消息
    latest_message = None
    for result in results:
        if isinstance(result, Exception) or result is None:
            continue

        if latest_message is None or result.created_at > latest_message.created_at:
            latest_message = result

    return latest_message


async def deep_search_location(bot, message, target_user):
    """深度搜索 - 当快速搜索没找到时使用"""
    guild = bot.get_guild(LOCATION_SERVER_ID)
    latest_message = None

    # 显示深度搜索信息
    search_msg = await message.channel.send(
        f"🔍 深度搜索用户 {target_user.mention} 的消息..."
    )

    # 按顺序搜索每个类别，但每个频道搜索更多消息
    for category_id in LOCATION_TARGET_CATEGORY_IDS:
        category = guild.get_channel(category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            continue

        for channel in category.channels:
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                # 搜索更多消息
                async for msg in channel.history(limit=500, oldest_first=False):
                    if msg.author.id == target_user.id:
                        if latest_message is None or msg.created_at > latest_message.created_at:
                            latest_message = msg
                        break  # 找到一条就继续下一个频道
            except:
                continue

            # 如果已经找到，可以提前退出
            if latest_message:
                break

    try:
        await search_msg.delete()
    except:
        pass

    if latest_message:
        # 更新缓存
        cache_key = f"{target_user.id}_{LOCATION_SERVER_ID}"
        user_location_cache[cache_key] = {
            'timestamp': datetime.now().timestamp(),
            'guild_id': latest_message.guild.id,
            'channel_id': latest_message.channel.id,
            'message_id': latest_message.id
        }

        message_link = f"https://discord.com/channels/{latest_message.guild.id}/{latest_message.channel.id}/{latest_message.id}"
        await message.channel.send(f"📍 {target_user.mention} 你现在在 [消息链接]({message_link})")
    else:
        await message.channel.send(f"❌ 未找到用户 {target_user.mention} 在指定类别中的消息记录")


async def handle_location_command(bot, message):
    """处理 /location 命令 - 快速查找用户在特定类别中的最后一次消息"""
    # 检查是否在指定服务器和类别中
    if message.guild.id != LOCATION_SERVER_ID:
        return

    if message.channel.category_id != LOCATION_CATEGORY_ID:
        return

    # 解析命令参数
    content = message.content.strip()
    args = content.split()

    # 确定目标用户
    target_user = None
    if len(args) > 1 and message.mentions:
        # 检查是否有提及用户
        for user in message.mentions:
            if user.id != bot.user.id:
                target_user = user
                break
    else:
        # 没有提及用户，使用消息发送者
        target_user = message.author

    if not target_user:
        target_user = message.author

    # 检查缓存
    cache_key = f"{target_user.id}_{LOCATION_SERVER_ID}"
    current_time = datetime.now().timestamp()

    if cache_key in user_location_cache:
        cache_data = user_location_cache[cache_key]
        if current_time - cache_data['timestamp'] < CACHE_DURATION:
            message_link = f"https://discord.com/channels/{cache_data['guild_id']}/{cache_data['channel_id']}/{cache_data['message_id']}"
            await message.channel.send(f"📍 {target_user.mention} 你现在在 [消息链接]({message_link})")
            return

    # 显示搜索信息
    search_msg = await message.channel.send(
        f"📍 正在快速搜索用户 {target_user.mention} 在指定类别中的最后一次消息..."
    )

    # 并行搜索所有类别
    guild = bot.get_guild(LOCATION_SERVER_ID)
    search_tasks = []

    for category_id in LOCATION_TARGET_CATEGORY_IDS:
        search_tasks.append(parallel_search_category(guild, category_id, target_user.id))

    # 并行执行所有类别搜索
    results = await asyncio.gather(*search_tasks, return_exceptions=True)

    # 找到最新的消息
    latest_message = None
    for result in results:
        if isinstance(result, Exception) or result is None:
            continue

        if latest_message is None or result.created_at > latest_message.created_at:
            latest_message = result

    try:
        await search_msg.delete()
    except:
        pass

    if latest_message:
        # 更新缓存
        user_location_cache[cache_key] = {
            'timestamp': current_time,
            'guild_id': latest_message.guild.id,
            'channel_id': latest_message.channel.id,
            'message_id': latest_message.id
        }

        message_link = f"https://discord.com/channels/{latest_message.guild.id}/{latest_message.channel.id}/{latest_message.id}"
        await message.channel.send(f"📍 {target_user.mention} 你现在在 [消息链接]({message_link})")
    else:
        # 如果没找到，再尝试更深入的搜索
        await deep_search_location(bot, message, target_user)


# ========== 特殊频道独立人设搜索（优化版） ==========
async def handle_special_character_search(bot, message, target_user):
    """专门处理控制频道 1238157595001491549 的人设搜索（仅搜索其映射的两个子区）"""
    search_channel_ids = config.CONTROL_TO_SEARCH_MAP.get(message.channel.id, [])
    if not search_channel_ids:
        await message.reply("⚠️ 该控制频道未配置搜索频道")
        return

    search_msg = await message.channel.send(
        f"🔍 正在子区中搜索提及用户 {target_user.mention} 的最早记录..."
    )

    earliest_message = None
    total_scanned = 0
    MAX_MESSAGES = 5000  # 每个子区最多扫描的消息数

    for channel_id in search_channel_ids:
        channel = bot.get_channel(channel_id)
        if not channel:
            await message.channel.send(f"⚠️ 无法获取子区 <#{channel_id}>")
            continue

        try:
            # 分页搜索，每次获取 100 条，从最早的消息开始
            last_id = None
            scanned = 0
            found = False

            while scanned < MAX_MESSAGES:
                # 获取下一批消息（从 last_id 之后，即更旧的消息）
                kwargs = {'limit': 100, 'oldest_first': True}
                if last_id:
                    kwargs['after'] = discord.Object(id=last_id)

                batch_messages = []
                async for msg in channel.history(**kwargs):
                    batch_messages.append(msg)
                    scanned += 1
                    total_scanned += 1
                    # 检查是否提及目标用户
                    if any(user.id == target_user.id for user in msg.mentions):
                        earliest_message = msg
                        found = True
                        break
                    # 更新 last_id 为当前消息的 ID，以便下一次获取更旧的消息
                    last_id = msg.id

                # 如果这一批没有消息，说明已到历史尽头
                if not batch_messages:
                    break

                if found:
                    break

            if found:
                break  # 找到最早的一条即可停止搜索其他子区

        except discord.Forbidden:
            await message.channel.send(f"⚠️ 无权限访问子区 {channel.mention}")
        except asyncio.TimeoutError:
            await message.channel.send(f"⚠️ 搜索子区 {channel.mention} 超时")
        except Exception as e:
            await message.channel.send(f"⚠️ 搜索子区 {channel.mention} 出错: {e}")

    await search_msg.delete()

    if earliest_message:
        link = f"https://discord.com/channels/{earliest_message.guild.id}/{earliest_message.channel.id}/{earliest_message.id}"
        await message.reply(f"哈吉米 {target_user.mention} 南北路多 {link}")
    else:
        await message.reply(
            f"❌ 在子区中未找到提及 {target_user.mention} 的消息（已扫描 {total_scanned} 条历史消息）。\n"
            "可能原因：\n"
            "• 机器人无权限读取该子区的完整历史\n"
            "• 提及消息过于久远，超出扫描上限\n"
            "• 提及格式未正确识别（请确认使用了 @提及）"
        )


# ========== 修改后的 handle_control_mention ==========
async def handle_control_mention(bot, message):
    """处理控制频道中的提及（优先判断特殊频道）"""
    # ---------- 特殊频道独立处理 ----------
    if message.channel.id == SPECIAL_CHAR_SEARCH_CHANNEL:
        # 仅支持“人设”命令
        args = message.content.split()
        if len(args) >= 2 and args[1] == "人设":
            if not message.mentions:
                temp = await message.reply("请提及要搜索的用户，例如: `@机器人 人设 @用户名`")
                await asyncio.sleep(10)
                try:
                    await temp.delete()
                except:
                    pass
                return
            target_user = None
            for user in message.mentions:
                if user.id != bot.user.id:
                    target_user = user
                    break
            if target_user:
                await handle_special_character_search(bot, message, target_user)
            else:
                await message.reply("请提及有效的用户")
            return
        else:
            # 该频道只允许人设搜索
            temp = await message.reply("⚠️ 该频道仅支持人设搜索（`@机器人 人设 @用户`）")
            await asyncio.sleep(10)
            try:
                await temp.delete()
            except:
                pass
            return

    # ---------- 以下是原有逻辑（其他控制频道） ----------
    # 检查是否是笨蛋特例
    if message.channel.id == IDIOT_TRIGGER_CHANNEL_ID:
        content = message.content.strip()
        if "笨蛋" in content:
            await message.reply(IDIOT_RESPONSE)
            return

    args = message.content.split()
    if len(args) < 2:
        temp_msg = await message.reply("请提供要搜索的用户名，例如: `@机器人 用户名` 或 `@机器人 人设 @用户名`")
        await asyncio.sleep(10)
        try:
            await temp_msg.delete()
        except:
            pass
        return

    # 检查是否是"人设"搜索（针对普通频道）
    is_character_search = False
    target_user = None

    if len(args) >= 2 and args[1] == "人设":
        is_character_search = True

        if not message.mentions:
            temp_msg = await message.reply("请提及要搜索的用户，例如: `@机器人 人设 @用户名`")
            await asyncio.sleep(10)
            try:
                await temp_msg.delete()
            except:
                pass
            return

        for user in message.mentions:
            if user.id != bot.user.id:
                target_user = user
                break

        if not target_user:
            temp_msg = await message.reply("请提及要搜索的用户，例如: `@机器人 人设 @用户名`")
            await asyncio.sleep(10)
            try:
                await temp_msg.delete()
            except:
                pass
            return
    else:
        search_username = ' '.join(args[1:]).strip()
        if len(search_username) < 2:
            temp_msg = await message.reply("⚠️ 用户名必须至少包含两个字符！")
            await asyncio.sleep(10)
            try:
                await temp_msg.delete()
            except:
                pass
            return

    # 根据频道类型执行不同的搜索（针对非特殊频道）
    if is_character_search and message.channel.id in CHARACTER_SEARCH_CHANNELS:
        await handle_character_search(bot, message, target_user)
    elif is_character_search:
        # 在非人设搜索频道使用了人设命令
        temp_msg = await message.reply("⚠️ 人设搜索功能只能在指定的频道使用！")
        await asyncio.sleep(10)
        try:
            await temp_msg.delete()
        except:
            pass
    else:
        await handle_normal_search(bot, message, search_username)


async def handle_non_control_mention(bot, message):
    """处理非控制频道中的提及"""
    # 先检查是否是中文对话频道
    if await handle_category_chat(bot, message):
        return

    # 如果不是中文对话频道，则提示用户去控制频道
    control_channels = ", ".join([f"<#{cid}>" for cid in config.CONTROL_CHANNEL_IDS])
    temp_msg = await message.reply(f"⚠️ 此命令只能在指定的控制频道使用！请前往: {control_channels}")
    await asyncio.sleep(10)
    try:
        await temp_msg.delete()
    except:
        pass
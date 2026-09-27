import discord
import asyncio
import config
import random
import re

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

# ========== 人设找人 ==========
DECOR = r'[\s>*_#`~|]*'
# 人设消息里名字的写法：「姓名：xx」「名字：xx」「昵称：xx」「名稱：xx」等
NAME_PATTERN = re.compile(
    rf'^{DECOR}(?:姓名|名字|名称|名稱|昵称|暱稱|昵稱){DECOR}[:：]{DECOR}(.+?){DECOR}$', re.M)


def parse_persona_name(content):
    """从人设消息中解析名字，不是人设消息时返回 None"""
    match = NAME_PATTERN.search(content)
    if match:
        return match.group(1)

    # 没写标签的人设：第一行直接是名字，下一行才是「性别：」等字段
    first_line = next((line for line in content.splitlines() if line.strip()), '')
    first_line = re.sub(rf'^{DECOR}|{DECOR}$', '', first_line)
    if first_line and len(first_line) <= 20 and not re.search(r'[:：]|<[@#]', first_line):
        return first_line
    return None


async def fetch_personas(bot):
    """拉取人设子区中的全部人设，返回 [(名字, 消息)]"""
    personas = []
    for thread_id in config.PERSONA_THREAD_IDS:
        # 已归档的子区不在缓存里，需要单独获取
        thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
        async for msg in thread.history(limit=None, oldest_first=True):
            name = parse_persona_name(msg.content)
            if name:
                personas.append((name, msg))
    return personas


async def handle_persona_search(bot, message, keyword):
    """按名字关键词搜索人设，列出所有命中的消息链接"""
    try:
        personas = await fetch_personas(bot)
    except discord.HTTPException as e:
        print(f"获取人设失败: {e}")
        await message.reply("⚠️ 读取人设区失败，请稍后再试")
        return

    keyword_lower = keyword.lower()
    matches = [(name, msg) for name, msg in personas if keyword_lower in name.lower()]

    if not matches:
        await message.reply(f"❌ 没有找到名字包含 `{keyword}` 的人设")
        return

    lines = [f"✅ 找到 {len(matches)} 个人设："]
    for name, msg in matches:
        line = f"• {name}：{msg.jump_url}"
        # Discord 单条消息上限 2000 字
        if len('\n'.join(lines + [line])) > 1900:
            lines.append(f"……还有 {len(matches) - len(lines) + 1} 个，请换个更精确的关键词")
            break
        lines.append(line)
    await message.reply('\n'.join(lines), allowed_mentions=discord.AllowedMentions.none())


async def handle_mention(bot, message):
    """处理人设服务器中 @玉猫 的消息"""
    # 去掉 @玉猫 本身，剩下的就是指令
    content = re.sub(rf'<@!?{bot.user.id}>', '', message.content).strip()

    if content.startswith('找'):
        keyword = content[1:].strip()
        if keyword:
            await handle_persona_search(bot, message, keyword)
            return

    temp_msg = await message.reply("用法：`@玉猫 找 名字`，例如 `@玉猫 找 沈墨`")
    await asyncio.sleep(10)
    try:
        await temp_msg.delete()
    except discord.HTTPException:
        pass

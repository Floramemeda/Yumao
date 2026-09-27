import discord
from discord.ext import commands
import config
import responses

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)


@bot.event
async def on_ready():
    print(f'登录成功: {bot.user.name}')
    print(f'控制频道: {config.CONTROL_CHANNEL_IDS}')
    print('控制频道与搜索频道映射:')
    for control_id, search_ids in config.CONTROL_TO_SEARCH_MAP.items():
        print(f'  {control_id} -> {search_ids}')



    # 发送上线通知
    await responses.send_online_notice(bot)


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # 处理中文对话频道消息（不要求提及机器人）
    if message.channel.category_id == responses.CHAT_CATEGORY_ID:
        await responses.handle_category_chat(bot, message)

    # 处理 /location 命令
    if message.content.startswith('/location'):
        await responses.handle_location_command(bot, message)

    # 处理控制频道的提及
    if bot.user.mentioned_in(message) and message.channel.id in config.CONTROL_CHANNEL_IDS:
        await responses.handle_control_mention(bot, message)
    # 处理非控制频道的提及
    elif bot.user.mentioned_in(message):
        await responses.handle_non_control_mention(bot, message)

    await bot.process_commands(message)

# 运行机器人（需在 main.py 中启动）
# if __name__ == "__main__":
#     bot.run(TOKEN)
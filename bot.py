import discord
from discord.ext import commands
import config
import responses

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# 小游戏扩展
EXTENSIONS = [
    'minigames.drinking_contest',
    'minigames.potato_heist',
    'minigames.pet_spark',
    'minigames.snowball_relay',
]


@bot.event
async def setup_hook():
    for ext in EXTENSIONS:
        await bot.load_extension(ext)
    synced = await bot.tree.sync()
    print(f'已同步 {len(synced)} 个 Slash Command')


@bot.event
async def on_ready():
    print(f'登录成功: {bot.user.name}')
    print(f'找人服务器: {config.PERSONA_GUILD_ID}')
    print(f'人设子区: {config.PERSONA_THREAD_IDS}')

    # 发送上线通知
    await responses.send_online_notice(bot)


@bot.event
async def on_message(message):
    # 忽略自己和私信
    if message.author == bot.user or message.guild is None:
        return

    # 处理中文对话频道消息（不要求提及机器人）
    if await responses.handle_category_chat(bot, message):
        return

    # 处理人设服务器中的 @玉猫（不用 mentioned_in，它会把 @everyone 也算进去）
    if message.guild.id == config.PERSONA_GUILD_ID and bot.user in message.mentions:
        await responses.handle_mention(bot, message)

    await bot.process_commands(message)

# 运行机器人（需在 main.py 中启动）
# if __name__ == "__main__":
#     bot.run(TOKEN)
import os
from dotenv import load_dotenv
from bot import bot

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

if __name__ == "__main__":
    bot.run(TOKEN)
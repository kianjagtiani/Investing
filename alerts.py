import os
import asyncio
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


def send_alert(chat_id: str, message: str):
    if not TELEGRAM_TOKEN or not chat_id:
        return

    async def _send():
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=chat_id, text=message)

    try:
        asyncio.run(_send())
    except Exception as e:
        print(f"Telegram alert failed: {e}")

from telethon import TelegramClient, events, Button
from telethon.tl.functions.channels import JoinChannelRequest
import asyncio
import re
import os

accounts = [
    {
        "session": "bot1",
        "api_id": 29484304,
        "api_hash": "53b687723c77668106677e6b35b7a0d7",
        "phone": "+998889910582"
    },
    {
        "session": "bot2",
        "api_id": 21191850,
        "api_hash": "9d67e81c4f88ea057b310af88c4bf264",
        "phone": "+998998092100"
    },
    {
        "session": "bot3",
        "api_id": 9607056,
        "api_hash": "fa07f654933fc56df8cd1e7e8565a6ea",
        "phone": "+998505880582"
    },
]

fruit_translations = {
    'Виноград': '🍇', 'Клубника': '🍓', 'Арбуз': '🍉', 'Ананас': '🍍',
    'Помидор': '🍅', 'Кокос': '🥥', 'Банан': '🍌', 'Яблоко': '🍎',
    'Персик': '🍑', 'Вишня': '🍒',
}

clients = []

for acc in accounts:
    client = TelegramClient(acc["session"], acc["api_id"], acc["api_hash"])
    clients.append((client, acc["phone"], acc["session"]))


async def clicker_bot(client, phone, session_name):
    try:
        session_file = f"{session_name}.session"
        if not os.path.exists(session_file):
            print(f"📲 [{phone}] - Bu raqamga Telegramdan kod keladi. Iltimos, kiritganingizda e'tiborli bo‘ling.")
            await client.start(phone=lambda: phone, code_callback=lambda: input(f"🔑 [{phone}] - Kodni kiriting: "))
        else:
            await client.start(phone=phone)

        print(f"✅ [{phone}] - ishga tushdi.")
    except Exception as e:
        print(f"❌ [{phone}] - Kirishda xatolik:", e)
        return

    bot = '@patrickstarsrobot'

    @client.on(events.NewMessage(from_users=bot))
    async def handler(event):
        try:
            message = event.message
            if message.buttons:
                # Obuna bo'lish
                if 'Подписаться' in message.raw_text:
                    sponsor_buttons = [b for row in message.buttons for b in row if 'Спонсор' in b.text]
                    for b in sponsor_buttons:
                        if b.url:
                            entity = await client.get_entity(b.url)
                            await client(JoinChannelRequest(entity))
                            print(f"📥 [{phone}] - Obuna bo'lindi: {entity.username if hasattr(entity, 'username') else entity.title}")
                    for row in message.buttons:
                        for btn in row:
                            if 'Проверить' in btn.text:
                                await asyncio.sleep(3)
                                await event.click(text=btn.text)
                                print(f"🔄 [{phone}] - Tekshiruv bosildi")
                                return

            if "награду" in message.message:
                match = re.search(r'где изображено «(.+?)»', message.message)
                if match:
                    fruit = match.group(1).strip()
                    emoji = fruit_translations.get(fruit)
                    if emoji:
                        for row in message.buttons:
                            for button in row:
                                if emoji in button.text:
                                    await asyncio.sleep(2)
                                    await event.click(text=button.text)
                                    print(f"✅ [{phone}] - Robot tekshiruvdan o'tdi: {fruit} → {emoji}")
                                    return
                    else:
                        print(f"⚠️ [{phone}] - Yangi meva topildi: {fruit}")
        except Exception as e:
            print(f"❌ [{phone}] - Xatolik (handler):", e)

    while True:
        try:
            await client.send_message(bot, '/start')
            await asyncio.sleep(3)
            async for message in client.iter_messages(bot, limit=5):

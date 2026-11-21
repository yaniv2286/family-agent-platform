import asyncio, os, sys
sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv
load_dotenv()
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    client = TelegramClient(
        StringSession(os.environ["SCOUT_TELEGRAM_SESSION"]),
        int(os.environ["SCOUT_TELEGRAM_API_ID"]),
        os.environ["SCOUT_TELEGRAM_API_HASH"])
    await client.connect()
    async for d in client.iter_dialogs():
        name = (d.name or "").lower()
        if any(k in name for k in ["bull", "bear", "שרון", "swing", "סווינג", "דוב"]):
            uname = getattr(d.entity, "username", None)
            print(f"FOUND: {d.name} | username={uname} | id={d.entity.id} | type={type(d.entity).__name__}")
    await client.disconnect()

asyncio.run(main())

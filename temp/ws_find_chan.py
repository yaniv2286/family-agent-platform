import asyncio, os, sys
sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv
load_dotenv()
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    api_id = int(os.environ["SCOUT_TELEGRAM_API_ID"])
    api_hash = os.environ["SCOUT_TELEGRAM_API_HASH"]
    session = os.environ["SCOUT_TELEGRAM_SESSION"]
    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    async for d in client.iter_dialogs():
        name = d.name or ""
        if any(k in name.lower() for k in ["מדר", "madar", "noam", "נועם"]):
            uname = getattr(d.entity, "username", None)
            print(f"FOUND: {name} | username={uname} | id={d.entity.id}")
    # also print all channel-ish names for a second look
    print("--- all broadcast channels ---")
    async for d in client.iter_dialogs():
        if getattr(d.entity, "broadcast", False):
            print(f"{d.name} | @{getattr(d.entity,'username',None)} | {d.entity.id}")
    await client.disconnect()

asyncio.run(main())

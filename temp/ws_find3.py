import asyncio, os, sys
sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv
load_dotenv()
from telethon import TelegramClient
from telethon.sessions import StringSession

KNOWN = set("@shlomiardanc,@TheAmericanDream5,@TheAmericanDream555,@YKalphatrading,@BullionClub,@SmartViewIL,@kranotgidur,@financenewsbyharel,@doronpeskin,@ilCrypt,@IsraelTax,@interactiveil,@TradingIL,@hatulfinancy,@TsuatYeterTLV,@SmartView3,@VC_seed,@TsuatYeterWS,@T20304050,@moneytalksahoy,@Yeda_Shave_Kesef,@tradingwithrayner,@InvestmentTalksIL,@WallstreetstocksUS,@investingwithvalue,@moneytalksuniverse,@misharbbursa,@Noaminvest".split(","))
KNOWN_IDS = {"2007708028","2465938273","2134533956","315386713","2802768520","1302869860"}
KEYS = ["שקע","invest","מני","בורס","trade","שוק","stock","מסחר","כסף","נדל","קריפט","bull","bear","דיביד","תיק "]

async def main():
    client = TelegramClient(
        StringSession(os.environ["SCOUT_TELEGRAM_SESSION"]),
        int(os.environ["SCOUT_TELEGRAM_API_ID"]),
        os.environ["SCOUT_TELEGRAM_API_HASH"])
    await client.connect()
    async for d in client.iter_dialogs():
        name = (d.name or "")
        uname = getattr(d.entity, "username", None)
        handle = f"@{uname}" if uname else str(d.entity.id)
        if handle in KNOWN or str(d.entity.id) in KNOWN_IDS or (uname and f"@{uname}" in KNOWN):
            continue
        if any(k in name.lower() for k in KEYS):
            print(f"{d.name} | {handle} | type={type(d.entity).__name__}")
    await client.disconnect()

asyncio.run(main())

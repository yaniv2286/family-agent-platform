"""One-time helper to create the SCOUT_TELEGRAM_SESSION string.

The Scout Ideas agent reads the Telegram channels the user subscribes to via
a user client (Telethon) - a bot can only see chats it was added to, so this
cannot reuse TELEGRAM_BOT_TOKEN.

Interactive usage (recommended for humans):

    python scripts/scout_telegram_auth.py

Non-interactive usage (for agent-driven flows). Credentials come from env
vars SCOUT_TELEGRAM_API_ID / SCOUT_TELEGRAM_API_HASH, or pass them as args:

    python scripts/scout_telegram_auth.py send <phone> [api_id api_hash]
    # Telegram sends a login code to your app; then:
    python scripts/scout_telegram_auth.py verify <phone> <code> <phone_code_hash> [password]

The session string it prints is a credential: treat it like a password,
never commit it. It goes into .env as SCOUT_TELEGRAM_SESSION on the server.
"""
import asyncio
import os
import sys


def _credentials(args):
    """Return (api_id, api_hash) from args or env vars."""
    if len(args) >= 2:
        return int(args[0]), args[1]
    api_id = os.getenv("SCOUT_TELEGRAM_API_ID")
    api_hash = os.getenv("SCOUT_TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit(
            "Set SCOUT_TELEGRAM_API_ID and SCOUT_TELEGRAM_API_HASH env vars, "
            "or pass them as arguments."
        )
    return int(api_id), api_hash


def _client(api_id, api_hash):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(), api_id, api_hash)


async def _interactive():
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        raise SystemExit("telethon is not installed. Run: pip install Telethon")

    api_id = int(input("api_id (from my.telegram.org): ").strip())
    api_hash = input("api_hash: ").strip()

    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        # client.start() interactively asks for phone + login code (+ 2FA).
        await client.start()
        _print_result(api_id, api_hash, client.session.save())


def _print_result(api_id, api_hash, session):
    print("\n" + "=" * 60)
    print("Add these to your .env on the server:")
    print("=" * 60)
    print(f"SCOUT_TELEGRAM_API_ID={api_id}")
    print(f"SCOUT_TELEGRAM_API_HASH={api_hash}")
    print(f"SCOUT_TELEGRAM_SESSION={session}")
    print("=" * 60)
    print("Keep the session string secret - it is a login credential.")


async def _send(phone, args):
    api_id, api_hash = _credentials(args)
    client = _client(api_id, api_hash)
    await client.connect()
    sent = await client.send_code_request(phone)
    print(f"PHONE_CODE_HASH={sent.phone_code_hash}")
    await client.disconnect()


async def _verify(phone, code, code_hash, password):
    api_id, api_hash = _credentials([])
    client = _client(api_id, api_hash)
    await client.connect()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
    except Exception as e:
        from telethon.errors import SessionPasswordNeededError
        if isinstance(e, SessionPasswordNeededError):
            if not password:
                raise SystemExit("Account has 2FA - rerun verify with the password as last arg")
            await client.sign_in(password=password)
        else:
            raise
    _print_result(api_id, api_hash, client.session.save())
    await client.disconnect()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        asyncio.run(_interactive())
    elif sys.argv[1] == "send" and len(sys.argv) >= 3:
        asyncio.run(_send(sys.argv[2], sys.argv[3:]))
    elif sys.argv[1] == "verify" and len(sys.argv) >= 5:
        password = sys.argv[5] if len(sys.argv) > 5 else None
        asyncio.run(_verify(sys.argv[2], sys.argv[3], sys.argv[4], password))
    else:
        raise SystemExit(__doc__)

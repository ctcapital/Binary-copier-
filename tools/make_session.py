"""Produce a portable Telegram session string.

Hosts without a persistent disk cannot keep `session/copier.session`, so the
login has to travel as a secret instead. Run this locally, once:

    ./.venv/bin/python tools/make_session.py

It reuses the login already stored in session/copier.session if there is one,
otherwise it asks for your phone, code and two-step password.

The string it prints is a LOGIN CREDENTIAL for your Telegram account — anyone
holding it can read your messages and act as you. Paste it straight into the
host's secrets store and nowhere else. Never commit it.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import TelegramClient  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

from copier import config, db  # noqa: E402


async def main() -> int:
    db.init()
    cfg = config.load()

    if not cfg.tg_api_id or not cfg.tg_api_hash:
        print("Set api_id and api_hash on the Connections page first.")
        return 1

    api_id, api_hash = int(cfg.tg_api_id), cfg.tg_api_hash

    # Prefer the existing on-disk login so there's nothing to re-authorise.
    existing = config.SESSION_PATH + ".session"
    if os.path.exists(existing):
        disk = TelegramClient(config.SESSION_PATH, api_id, api_hash)
        await disk.connect()
        if await disk.is_user_authorized():
            me = await disk.get_me()
            string = StringSession.save(StringSession())  # placeholder, replaced below
            # Re-key the authorised connection into a StringSession.
            string = StringSession()
            string.set_dc(disk.session.dc_id, disk.session.server_address,
                          disk.session.port)
            string.auth_key = disk.session.auth_key
            print("\nReusing the login already in session/copier.session")
            print("Signed in as: {} (@{})".format(
                (me.first_name or "").strip(), me.username or "no username"))
            await disk.disconnect()
            emit(string.save())
            return 0
        await disk.disconnect()

    print("No existing login found — signing in fresh.")
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start(phone=lambda: input("Phone (+countrycode…): "))
    me = await client.get_me()
    print("Signed in as: {} (@{})".format(
        (me.first_name or "").strip(), me.username or "no username"))
    emit(client.session.save())
    await client.disconnect()
    return 0


def emit(string: str) -> None:
    print("\n" + "=" * 70)
    print("TG_SESSION_STRING  ({} chars)".format(len(string)))
    print("=" * 70)
    print(string)
    print("=" * 70)
    print("\nThis grants full access to your Telegram account.")
    print("Paste it into your host's secrets store only. Never commit it.")
    print("Revoke it any time from Telegram → Settings → Devices.")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

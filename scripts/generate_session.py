#!/usr/bin/env python3
"""Generate a Pyrogram session locally; login secrets never enter a bot chat."""
import asyncio
import getpass

from pyrogram import Client


async def main():
    print("RestrictiveDL secure local session generator")
    print("Run this only on a trusted device. Telegram may request a login code and 2FA password here.")
    api_id = int(input("API ID: ").strip())
    api_hash = getpass.getpass("API hash (hidden): ").strip()
    phone = input("Phone in international format (for example +989...): ").strip()
    client = Client(
        "restdl_setup", api_id=api_id, api_hash=api_hash,
        phone_number=phone, in_memory=True, no_updates=True,
    )
    try:
        # Pyrogram prompts in this terminal for the delivered code and, when
        # enabled, the account's two-step-verification password.
        await client.start()
        me = await client.get_me()
        session = await client.export_session_string()
        print(f"\nAuthorized as {me.first_name} (@{me.username or '-'})")
        print("\nSESSION STRING (treat this like a password):\n")
        print(session)
        print("\nImport it through Account → Connect in the bot, then clear this terminal history.")
    finally:
        if getattr(client, "is_connected", False):
            await client.stop()


if __name__ == "__main__":
    asyncio.run(main())

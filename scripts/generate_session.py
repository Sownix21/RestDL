#!/usr/bin/env python3
"""Generate a Pyrogram session in a trusted terminal."""
import asyncio
import argparse
import getpass
import os
import sys

from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded


def terminal_prompt(prompt: str, secret: bool = False) -> str:
    """Read from the controlling terminal even when install.sh is piped via curl."""
    if os.name != "nt" and os.path.exists("/dev/tty"):
        with open("/dev/tty", "r+", encoding="utf-8", errors="replace") as terminal:
            if secret:
                return getpass.getpass(prompt, stream=terminal).strip()
            terminal.write(prompt)
            terminal.flush()
            return terminal.readline().strip()
    return (getpass.getpass(prompt) if secret else input(prompt)).strip()


async def authorize(api_id: int, api_hash: str):
    phone = terminal_prompt("Admin phone in international format (for example +989...): ")
    client = Client(
        "restdl_admin_setup", api_id=api_id, api_hash=api_hash,
        in_memory=True, no_updates=True, workers=1,
    )
    try:
        await client.connect()
        sent_code = await client.send_code(phone)
        user = None
        for _ in range(3):
            code = terminal_prompt("Telegram login code: ").replace("-", "").replace(" ", "")
            try:
                user = await client.sign_in(phone, sent_code.phone_code_hash, code)
                break
            except SessionPasswordNeeded:
                for _ in range(3):
                    password = terminal_prompt("Telegram two-step-verification password: ", secret=True)
                    try:
                        user = await client.check_password(password)
                        break
                    except Exception as exc:
                        print(f"Two-step password rejected: {exc}", file=sys.stderr)
                break
            except Exception as exc:
                if exc.__class__.__name__ in {"PhoneCodeInvalid", "PhoneCodeEmpty"}:
                    print("Login code rejected; try again.", file=sys.stderr)
                    continue
                raise
        if not user or not getattr(user, "id", None):
            raise RuntimeError("Telegram authorization was not completed")
        await client.initialize()
        session = await client.export_session_string()
        me = await client.get_me()
        return session, me
    finally:
        if getattr(client, "is_connected", False):
            if getattr(client, "is_initialized", False):
                await client.stop()
            else:
                await client.disconnect()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--machine", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.machine:
        api_id = int(os.environ["RESTDL_SETUP_API_ID"])
        api_hash = os.environ["RESTDL_SETUP_API_HASH"]
        print("Authorizing the Linux-managed administrator account...", file=sys.stderr)
    else:
        print("RestrictiveDL secure session generator")
        print("Run this only on a trusted device. Login secrets remain in this terminal.")
        api_id = int(terminal_prompt("API ID: "))
        api_hash = terminal_prompt("API hash (hidden): ", secret=True)

    session, me = await authorize(api_id, api_hash)
    if args.machine:
        print(f"Authorized admin as {me.first_name} (@{me.username or '-'})", file=sys.stderr)
        print(f"{me.id}:{session}")
    else:
        print(f"\nAuthorized as {me.first_name} (@{me.username or '-'})")
        print("\nSESSION STRING (treat this like a password):\n")
        print(session)


if __name__ == "__main__":
    asyncio.run(main())

"""Reliable chat resolution for Pyrogram user sessions."""
import asyncio
from typing import Union

from logger import get_logger
from helpers.msg import parse_chat_identifier

logger = get_logger(__name__)
ChatIdentifier = Union[int, str]
_dialog_refresh_lock = asyncio.Lock()


def _candidate_ids(identifier: ChatIdentifier):
    parsed = parse_chat_identifier(identifier)
    candidates = [parsed]
    # A copied bare channel ID often omits Telegram's -100 prefix. Do not
    # destroy the positive form (it may be a private chat); try both safely.
    if isinstance(parsed, int) and parsed > 0:
        channel_id = int(f"-100{parsed}")
        if channel_id not in candidates:
            candidates.append(channel_id)
    return candidates


async def resolve_chat(client, identifier: ChatIdentifier):
    """Resolve a chat, hydrating Pyrogram's peer cache from account dialogs.

    Pyrogram can raise PEER_ID_INVALID for a numeric chat that the account is a
    member of when that peer has not yet been loaded into the current session.
    Iterating dialogs populates the access-hash cache, after which get_chat and
    history calls work consistently.
    """
    candidates = _candidate_ids(identifier)
    first_error = None
    for candidate in candidates:
        try:
            return await client.get_chat(candidate)
        except Exception as exc:
            first_error = first_error or exc

    async with _dialog_refresh_lock:
        matched = None
        try:
            async for dialog in client.get_dialogs(limit=0):
                chat = dialog.chat
                if chat.id in candidates:
                    matched = chat
                    break
                username = getattr(chat, "username", None)
                if username and any(
                    isinstance(item, str) and item.lstrip("@").casefold() == username.casefold()
                    for item in candidates
                ):
                    matched = chat
                    break
        except Exception as exc:
            logger.warning("Could not refresh Telegram dialogs while resolving %r: %s", identifier, exc)
        if matched is not None:
            return matched

        for candidate in candidates:
            try:
                return await client.get_chat(candidate)
            except Exception:
                pass

    raise first_error or ValueError(f"Unable to resolve chat: {identifier}")


def preferred_chat_reference(chat):
    """Return a stable reference suitable for history/message API calls."""
    return chat.id


def chat_title(chat) -> str:
    return getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(chat.id)


__all__ = ["resolve_chat", "preferred_chat_reference", "chat_title"]

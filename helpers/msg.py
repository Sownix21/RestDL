# helpers/msg.py
import re
from typing import Any, Optional, Tuple, Union
from urllib.parse import parse_qs, urlparse

ChatIdentifier = Union[int, str]
TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.dog"}


def _parse_telegram_url(value: str):
    """Return a parsed Telegram URL, accepting the common host aliases."""
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parsed.scheme.lower() not in {"http", "https"} or host not in TELEGRAM_HOSTS:
        raise ValueError(f"Invalid Telegram URL: {value}")
    return parsed


def _private_link_chat_id(value: str) -> int:
    """Convert the /c/<id> URL component to Telegram's internal -100 ID."""
    value = value.strip()
    if value.startswith("-100") and value[4:].isdigit():
        return int(value)
    if value.isdigit():
        return int(f"-100{value}")
    raise ValueError(f"Invalid private chat ID: {value}")


def getChatMsgID(url: str) -> Tuple[ChatIdentifier, int]:
    """Extract a chat reference and message ID from a Telegram post URL.

    Supports public, private, discussion-topic and public preview links on all
    standard Telegram short-link domains.
    """
    parsed = _parse_telegram_url(url)
    parts = [part for part in parsed.path.split("/") if part]

    # https://t.me/c/123456789/42 and topic form /c/123456789/7/42
    if len(parts) >= 3 and parts[0].lower() == "c":
        if not parts[-1].isdigit():
            raise ValueError(f"Invalid message ID in URL: {url}")
        return _private_link_chat_id(parts[1]), int(parts[-1])

    # Public web-preview form: https://t.me/s/channel/42
    if len(parts) >= 3 and parts[0].lower() == "s":
        if not parts[-1].isdigit():
            raise ValueError(f"Invalid message ID in URL: {url}")
        return f"@{parts[1].lstrip('@')}", int(parts[-1])

    # Public post and topic forms: /channel/42 and /channel/7/42
    if len(parts) >= 2 and parts[0].lower() not in {"joinchat", "addlist"}:
        if parts[-1].isdigit() and not (len(parts) >= 3 and parts[1].lower() == "s"):
            return f"@{parts[0].lstrip('@')}", int(parts[-1])

    raise ValueError(f"Invalid post URL: {url}")


def getStoryChatMsgID(url: str) -> Tuple[str, int]:
    """Extract a username and story ID from /<username>/s/<id>."""
    parsed = _parse_telegram_url(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 3 or parts[1].lower() != "s" or not parts[2].isdigit():
        raise ValueError(f"Invalid story URL: {url}")
    return f"@{parts[0].lstrip('@')}", int(parts[2])


def is_story_link(url: str) -> bool:
    try:
        getStoryChatMsgID(url)
        return True
    except (TypeError, ValueError):
        return False


def is_valid_telegram_url(url: str) -> bool:
    try:
        _parse_telegram_url(url)
        return True
    except (AttributeError, TypeError, ValueError):
        return False


def extract_urls(text: str) -> list:
    """Extract Telegram HTTP links without common trailing punctuation."""
    pattern = r"https?://(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>]+"
    return [item.rstrip(".,;:!?)]}\")'") for item in re.findall(pattern, text, re.I)]


def parse_chat_identifier(chat_id: ChatIdentifier) -> ChatIdentifier:
    """Normalize an ID, username, Telegram link, or tg://resolve reference.

    Positive numeric IDs stay positive because they can represent users/private
    chats. Resolution code may additionally try the corresponding -100 channel
    ID when the positive form cannot be found.
    """
    if isinstance(chat_id, int):
        return chat_id
    if not isinstance(chat_id, str):
        raise ValueError("Chat identifier must be a string or integer")

    value = chat_id.strip()
    if not value:
        raise ValueError("Chat identifier cannot be empty")

    if value.lower().startswith("tg://resolve"):
        domain = parse_qs(urlparse(value).query).get("domain", [""])[0].strip()
        if not domain:
            raise ValueError("Telegram resolve link has no domain")
        return f"@{domain.lstrip('@')}"

    if is_valid_telegram_url(value):
        parsed = _parse_telegram_url(value)
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError("Telegram URL has no chat identifier")
        if parts[0].lower() == "c" and len(parts) >= 2:
            return _private_link_chat_id(parts[1])
        if parts[0].lower() == "s" and len(parts) >= 2:
            return f"@{parts[1].lstrip('@')}"
        if parts[0].lower() in {"joinchat", "+"} or parts[0].startswith("+"):
            return value
        return f"@{parts[0].lstrip('@')}"

    cleaned = value.lstrip("@")
    if cleaned.lstrip("-").isdigit():
        return int(cleaned)
    if any(char.isspace() for char in cleaned):
        raise ValueError("Usernames cannot contain spaces")
    return f"@{cleaned}"


def get_chat_type(chat_id: ChatIdentifier) -> str:
    try:
        parsed = parse_chat_identifier(chat_id)
    except ValueError:
        return "unknown"
    return "numeric" if isinstance(parsed, int) else "username"


def parse_chat_id_direct(identifier: str) -> Tuple[ChatIdentifier, Optional[int]]:
    """Parse a chat identifier or post/story URL and optional message ID."""
    value = identifier.strip()
    if not value:
        raise ValueError("Chat identifier cannot be empty")
    if is_valid_telegram_url(value):
        try:
            return getChatMsgID(value)
        except ValueError:
            try:
                return getStoryChatMsgID(value)
            except ValueError:
                return parse_chat_identifier(value), None
    return parse_chat_identifier(value), None


def format_message_link(chat_id: ChatIdentifier, message_id: int) -> str:
    """Format a valid public/private message link when possible."""
    parsed = parse_chat_identifier(chat_id)
    if isinstance(parsed, str):
        if parsed.startswith("http"):
            return parsed
        return f"https://t.me/{parsed.lstrip('@')}/{int(message_id)}"
    value = str(parsed)
    if value.startswith("-100") and value[4:].isdigit():
        return f"https://t.me/c/{value[4:]}/{int(message_id)}"
    return f"tg://openmessage?chat_id={parsed}&message_id={int(message_id)}"


def format_story_link(chat_id: ChatIdentifier, story_id: int) -> str:
    value = str(parse_chat_identifier(chat_id)).lstrip("@")
    return f"https://t.me/{value}/s/{int(story_id)}"


def debug_url(url: str) -> dict:
    result = {"url": url, "is_valid": False, "type": "unknown", "chat_id": None,
              "message_id": None, "error": None}
    try:
        if is_story_link(url):
            result["type"] = "story"
            result["chat_id"], result["message_id"] = getStoryChatMsgID(url)
        else:
            result["type"] = "post"
            result["chat_id"], result["message_id"] = getChatMsgID(url)
        result["is_valid"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def get_file_name(message_id: int, chat_message: Any) -> str:
    if chat_message.document:
        return chat_message.document.file_name or f"document_{message_id}.bin"
    if chat_message.video:
        return f"video_{message_id}.mp4"
    if chat_message.audio:
        return f"audio_{message_id}.mp3"
    if chat_message.photo:
        return f"photo_{message_id}.jpg"
    if chat_message.voice:
        return f"voice_{message_id}.ogg"
    if chat_message.video_note:
        return f"video_note_{message_id}.mp4"
    if chat_message.animation:
        return f"animation_{message_id}.gif"
    if chat_message.sticker:
        if getattr(chat_message.sticker, "is_animated", False):
            return f"sticker_{message_id}.tgs"
        if getattr(chat_message.sticker, "is_video", False):
            return f"sticker_{message_id}.webm"
        return f"sticker_{message_id}.webp"
    return f"file_{message_id}.bin"


def get_story_file_name(story_id: int, story: Any, username: str) -> str:
    return f"story_{username}_{story_id}.{'jpg' if story.photo else 'mp4'}"


def get_raw_text(text: Optional[str], entities: Optional[list]) -> Tuple[str, list]:
    return text or "", entities or []


def is_chat_id(identifier: str) -> bool:
    try:
        parse_chat_identifier(identifier)
        return not is_valid_telegram_url(identifier)
    except (TypeError, ValueError):
        return False


parse_chat_id = parse_chat_identifier

__all__ = [
    "getChatMsgID", "getStoryChatMsgID", "is_story_link",
    "is_valid_telegram_url", "extract_urls", "parse_chat_identifier",
    "parse_chat_id", "get_chat_type", "parse_chat_id_direct",
    "format_message_link", "format_story_link", "debug_url", "get_file_name",
    "get_story_file_name", "get_raw_text", "is_chat_id",
]

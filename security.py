"""Encryption helpers for user-owned Telegram credentials and sessions."""
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from config import Config


class SecretBox:
    def __init__(self, key: Optional[str] = None):
        raw_key = (key or Config.SESSION_ENCRYPTION_KEY).strip()
        self._fernet = Fernet(raw_key.encode("ascii")) if raw_key else None

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def encrypt(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not self._fernet:
            raise RuntimeError("SESSION_ENCRYPTION_KEY is not configured")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not self._fernet:
            raise RuntimeError("SESSION_ENCRYPTION_KEY is not configured")
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("Stored credential cannot be decrypted with the configured key") from exc


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


secret_box = SecretBox()

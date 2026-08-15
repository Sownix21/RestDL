"""Lifecycle manager for isolated, encrypted per-user Pyrogram sessions."""
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional

from pyrogram import Client
from pyrogram.types import LoginToken, User

from database import Database
from logger import get_logger
from security import SecretBox, secret_box

logger = get_logger(__name__)


@dataclass
class ConnectedAccount:
    client: Client
    telegram_user_id: int
    username: Optional[str]
    display_name: str


class UserSessionManager:
    def __init__(self, database: Database, box: SecretBox = secret_box):
        self.db = database
        self.box = box
        self._accounts: Dict[int, ConnectedAccount] = {}
        self._pending_qr = {}
        self._locks: Dict[int, asyncio.Lock] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def import_session(self, user_id: int, api_id: int, api_hash: str,
                             session_string: str) -> ConnectedAccount:
        if not self.box.available:
            raise RuntimeError("Server-side session encryption is not configured")
        if api_id <= 0 or len(api_hash.strip()) < 16 or len(session_string.strip()) < 40:
            raise ValueError("API credentials or session string are malformed")

        async with self._lock(user_id):
            await self._disconnect_unlocked(user_id)
            client = Client(
                f"account_{user_id}", api_id=api_id, api_hash=api_hash.strip(),
                session_string=session_string.strip(), in_memory=True,
                no_updates=True, workers=1, max_concurrent_transmissions=1,
            )
            try:
                await client.start()
                me = await client.get_me()
                account = ConnectedAccount(
                    client=client,
                    telegram_user_id=me.id,
                    username=me.username,
                    display_name=" ".join(part for part in [me.first_name, me.last_name] if part),
                )
                self.db.save_telegram_credential(
                    user_id,
                    api_id_encrypted=self.box.encrypt(str(api_id)),
                    api_hash_encrypted=self.box.encrypt(api_hash.strip()),
                    session_encrypted=self.box.encrypt(await client.export_session_string()),
                    telegram_user_id=str(me.id), telegram_username=me.username,
                    phone_hint=(me.phone_number[-4:] if getattr(me, "phone_number", None) else None),
                    status="active",
                )
                self._accounts[user_id] = account
                return account
            except Exception:
                if getattr(client, "is_connected", False):
                    await client.stop()
                raise

    async def begin_qr(self, user_id: int, api_id: int, api_hash: str):
        """Create a QR authorization token without collecting a login code."""
        if not self.box.available:
            raise RuntimeError("Server-side session encryption is not configured")
        async with self._lock(user_id):
            previous = self._pending_qr.pop(user_id, None)
            if previous and getattr(previous[0], "is_connected", False):
                await previous[0].disconnect()
            client = Client(
                f"qr_{user_id}", api_id=api_id, api_hash=api_hash,
                in_memory=True, no_updates=True, workers=1,
            )
            try:
                await client.connect()
                result = await client.sign_in_qrcode()
                if not isinstance(result, LoginToken):
                    raise RuntimeError("Unexpected QR authorization response")
            except Exception:
                if getattr(client, "is_connected", False):
                    await client.disconnect()
                raise
            self._pending_qr[user_id] = (client, api_id, api_hash)
            return result

    async def complete_qr(self, user_id: int) -> Optional[ConnectedAccount]:
        async with self._lock(user_id):
            pending = self._pending_qr.get(user_id)
            if not pending:
                raise RuntimeError("QR setup expired; start again")
            client, api_id, api_hash = pending
            result = await client.sign_in_qrcode()
            if isinstance(result, LoginToken):
                return None
            if not isinstance(result, User):
                raise RuntimeError("Unexpected QR authorization result")
            await client.initialize()
            account = ConnectedAccount(
                client, result.id, result.username,
                " ".join(part for part in [result.first_name, result.last_name] if part),
            )
            self.db.save_telegram_credential(
                user_id,
                api_id_encrypted=self.box.encrypt(str(api_id)),
                api_hash_encrypted=self.box.encrypt(api_hash),
                session_encrypted=self.box.encrypt(await client.export_session_string()),
                telegram_user_id=str(result.id), telegram_username=result.username,
                phone_hint=None, status="active",
            )
            self._pending_qr.pop(user_id, None)
            self._accounts[user_id] = account
            return account

    async def get(self, user_id: int) -> Optional[ConnectedAccount]:
        if user_id in self._accounts:
            return self._accounts[user_id]
        credential = self.db.get_telegram_credential(user_id)
        if not credential or credential.status != "active":
            return None
        async with self._lock(user_id):
            if user_id in self._accounts:
                return self._accounts[user_id]
            try:
                api_id = int(self.box.decrypt(credential.api_id_encrypted))
                api_hash = self.box.decrypt(credential.api_hash_encrypted)
                session_string = self.box.decrypt(credential.session_encrypted)
                client = Client(
                    f"account_{user_id}", api_id=api_id, api_hash=api_hash,
                    session_string=session_string, in_memory=True, no_updates=True,
                    workers=1, max_concurrent_transmissions=1,
                )
                await client.start()
                me = await client.get_me()
                account = ConnectedAccount(
                    client, me.id, me.username,
                    " ".join(part for part in [me.first_name, me.last_name] if part),
                )
                self._accounts[user_id] = account
                return account
            except Exception as exc:
                logger.error("Could not restore session for bot user %s: %s", user_id, exc)
                self.db.save_telegram_credential(
                    user_id,
                    api_id_encrypted=credential.api_id_encrypted,
                    api_hash_encrypted=credential.api_hash_encrypted,
                    session_encrypted=credential.session_encrypted,
                    telegram_user_id=credential.telegram_user_id,
                    telegram_username=credential.telegram_username,
                    phone_hint=credential.phone_hint,
                    status="invalid",
                )
                return None

    async def disconnect(self, user_id: int, erase: bool = True):
        async with self._lock(user_id):
            await self._disconnect_unlocked(user_id)
            if erase:
                self.db.delete_telegram_credential(user_id)

    async def _disconnect_unlocked(self, user_id: int):
        pending = self._pending_qr.pop(user_id, None)
        if pending and getattr(pending[0], "is_connected", False):
            try:
                await pending[0].disconnect()
            except Exception as exc:
                logger.warning("Failed to close pending QR client %s: %s", user_id, exc)
        account = self._accounts.pop(user_id, None)
        if account and getattr(account.client, "is_connected", False):
            try:
                await account.client.stop()
            except Exception as exc:
                logger.warning("Failed to stop user client %s: %s", user_id, exc)

    async def close_all(self):
        for user_id in list(self._accounts):
            async with self._lock(user_id):
                await self._disconnect_unlocked(user_id)
        for user_id, (client, _, _) in list(self._pending_qr.items()):
            if getattr(client, "is_connected", False):
                await client.disconnect()
            self._pending_qr.pop(user_id, None)

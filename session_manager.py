"""Lifecycle manager for isolated, encrypted per-user Pyrogram sessions."""
import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, Optional

from pyrogram import Client
from pyrogram.types import LoginToken, User

from database import Database
from logger import get_logger
from security import SecretBox, secret_box
from config import Config

logger = get_logger(__name__)


@dataclass
class ConnectedAccount:
    client: Client
    telegram_user_id: int
    username: Optional[str]
    display_name: str
    last_used: float = 0.0


@dataclass
class PendingPhoneLogin:
    client: Client
    api_id: int
    api_hash: str
    phone_number: str
    phone_code_hash: str
    awaiting_password: bool = False
    created_at: float = 0.0


class UserSessionManager:
    def __init__(self, database: Database, box: SecretBox = secret_box):
        self.db = database
        self.box = box
        self._accounts: Dict[int, ConnectedAccount] = {}
        self._pending_qr = {}
        self._pending_phone: Dict[int, PendingPhoneLogin] = {}
        self._external_accounts = set()
        self._locks: Dict[int, asyncio.Lock] = {}
        self._qr_created_at = {}
        self._auth_attempts = defaultdict(deque)

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    def active_user_ids(self):
        return set(self._accounts)

    def touch(self, user_id: int) -> None:
        """Keep a client alive while it is serving an active download job."""
        account = self._accounts.get(user_id)
        if account:
            account.last_used = time.monotonic()

    async def _ensure_capacity(self, incoming_user_id: int) -> None:
        ordinary = [
            (account.last_used, user_id) for user_id, account in self._accounts.items()
            if user_id not in self._external_accounts and user_id != incoming_user_id
        ]
        if len(ordinary) < Config.MAX_ACTIVE_USER_SESSIONS:
            return
        _, victim_id = min(ordinary)
        async with self._lock(victim_id):
            await self._disconnect_unlocked(victim_id)

    async def attach_external(self, user_id: int, client: Client) -> ConnectedAccount:
        """Attach the Linux-configured admin client without duplicating its session."""
        me = await client.get_me()
        if me.id != user_id:
            raise RuntimeError(
                f"Configured administrator ID {user_id} does not match authorized account {me.id}"
            )
        account = ConnectedAccount(
            client, me.id, me.username,
            " ".join(part for part in [me.first_name, me.last_name] if part),
            time.monotonic(),
        )
        self._accounts[user_id] = account
        self._external_accounts.add(user_id)
        return account

    def _register_auth_attempt(self, user_id: int):
        now = time.monotonic()
        attempts = self._auth_attempts[user_id]
        while attempts and now - attempts[0] > 3600:
            attempts.popleft()
        if len(attempts) >= Config.AUTH_ATTEMPTS_PER_HOUR:
            raise RuntimeError("Too many authorization attempts; try again later")
        if (len(self._pending_qr) + len(self._pending_phone) >= Config.MAX_PENDING_AUTH
                and user_id not in self._pending_qr and user_id not in self._pending_phone):
            raise RuntimeError("The authorization service is busy; try again shortly")
        attempts.append(now)

    async def import_session(self, user_id: int, api_id: int, api_hash: str,
                             session_string: str) -> ConnectedAccount:
        if not self.box.available:
            raise RuntimeError("Server-side session encryption is not configured")
        if api_id <= 0 or len(api_hash.strip()) < 16 or len(session_string.strip()) < 40:
            raise ValueError("API credentials or session string are malformed")
        if len(session_string.strip()) > 4096:
            raise ValueError("Session string is unexpectedly large")
        self._register_auth_attempt(user_id)

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
                    last_used=time.monotonic(),
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
                await self._ensure_capacity(user_id)
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
        self._register_auth_attempt(user_id)
        async with self._lock(user_id):
            await self._close_pending_unlocked(user_id)
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
            self._qr_created_at[user_id] = time.monotonic()
            return result

    async def complete_qr(self, user_id: int):
        async with self._lock(user_id):
            pending = self._pending_qr.get(user_id)
            if not pending:
                raise RuntimeError("QR setup expired; start again")
            client, api_id, api_hash = pending
            result = await client.sign_in_qrcode()
            if isinstance(result, LoginToken):
                return result
            if not isinstance(result, User):
                raise RuntimeError("Unexpected QR authorization result")
            await client.initialize()
            account = ConnectedAccount(
                client, result.id, result.username,
                " ".join(part for part in [result.first_name, result.last_name] if part),
                time.monotonic(),
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
            self._qr_created_at.pop(user_id, None)
            await self._ensure_capacity(user_id)
            self._accounts[user_id] = account
            return account

    async def submit_qr_password(self, user_id: int, password: str) -> ConnectedAccount:
        async with self._lock(user_id):
            pending = self._pending_qr.get(user_id)
            if not pending:
                raise RuntimeError("QR login expired; start again")
            client, api_id, api_hash = pending
            result = await client.check_password(password)
            await client.initialize()
            account = ConnectedAccount(
                client, result.id, result.username,
                " ".join(part for part in [result.first_name, result.last_name] if part),
                time.monotonic(),
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
            self._qr_created_at.pop(user_id, None)
            await self._ensure_capacity(user_id)
            self._accounts[user_id] = account
            return account

    async def begin_phone(self, user_id: int, api_id: int, api_hash: str,
                          phone_number: str):
        """Send a login code and retain the unauthorised client in memory."""
        if not self.box.available:
            raise RuntimeError("Server-side session encryption is not configured")
        self._register_auth_attempt(user_id)
        normalized = phone_number.strip().replace(" ", "")
        if not normalized.startswith("+") or not normalized[1:].isdigit():
            raise ValueError("Use international format, for example +989123456789")
        async with self._lock(user_id):
            await self._close_pending_unlocked(user_id)
            client = Client(
                f"phone_{user_id}", api_id=api_id, api_hash=api_hash.strip(),
                in_memory=True, no_updates=True, workers=1,
                max_concurrent_transmissions=1,
            )
            try:
                await client.connect()
                sent_code = await client.send_code(normalized)
            except Exception:
                if getattr(client, "is_connected", False):
                    await client.disconnect()
                raise
            self._pending_phone[user_id] = PendingPhoneLogin(
                client, api_id, api_hash.strip(), normalized,
                sent_code.phone_code_hash,
                created_at=time.monotonic(),
            )
            return sent_code

    async def submit_phone_code(self, user_id: int, code: str):
        """Submit a code collected through callback buttons, never a chat message."""
        async with self._lock(user_id):
            pending = self._pending_phone.get(user_id)
            if not pending:
                raise RuntimeError("Phone login expired; start again")
            try:
                result = await pending.client.sign_in(
                    pending.phone_number, pending.phone_code_hash, code
                )
            except Exception as exc:
                # Keep the connected client for a 2FA password or a corrected code.
                if exc.__class__.__name__ == "SessionPasswordNeeded":
                    pending.awaiting_password = True
                raise
            return await self._finalize_pending_phone(user_id, pending, result)

    async def submit_password(self, user_id: int, password: str) -> ConnectedAccount:
        async with self._lock(user_id):
            pending = self._pending_phone.get(user_id)
            if not pending or not pending.awaiting_password:
                raise RuntimeError("No two-step-verification login is waiting")
            result = await pending.client.check_password(password)
            return await self._finalize_pending_phone(user_id, pending, result)

    async def _finalize_pending_phone(self, user_id: int, pending: PendingPhoneLogin,
                                      telegram_user: User) -> ConnectedAccount:
        await pending.client.initialize()
        account = ConnectedAccount(
            pending.client, telegram_user.id, telegram_user.username,
            " ".join(part for part in [telegram_user.first_name, telegram_user.last_name] if part),
            time.monotonic(),
        )
        self.db.save_telegram_credential(
            user_id,
            api_id_encrypted=self.box.encrypt(str(pending.api_id)),
            api_hash_encrypted=self.box.encrypt(pending.api_hash),
            session_encrypted=self.box.encrypt(await pending.client.export_session_string()),
            telegram_user_id=str(telegram_user.id),
            telegram_username=telegram_user.username,
            phone_hint=pending.phone_number[-4:], status="active",
        )
        self._pending_phone.pop(user_id, None)
        await self._ensure_capacity(user_id)
        self._accounts[user_id] = account
        return account

    async def _close_pending_unlocked(self, user_id: int):
        qr = self._pending_qr.pop(user_id, None)
        self._qr_created_at.pop(user_id, None)
        phone = self._pending_phone.pop(user_id, None)
        for client in [qr[0] if qr else None, phone.client if phone else None]:
            if client and getattr(client, "is_connected", False):
                try:
                    await client.disconnect()
                except Exception as exc:
                    logger.warning("Failed to close pending auth client %s: %s", user_id, exc)

    async def get(self, user_id: int) -> Optional[ConnectedAccount]:
        if user_id in self._accounts:
            self._accounts[user_id].last_used = time.monotonic()
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
                    time.monotonic(),
                )
                await self._ensure_capacity(user_id)
                self._accounts[user_id] = account
                return account
            except Exception as exc:
                logger.error("Could not restore session for bot user %s: %s", user_id, exc)
                if 'client' in locals() and getattr(client, "is_connected", False):
                    try:
                        await client.stop()
                    except Exception:
                        pass
                permanent_errors = {
                    "AuthKeyUnregistered", "AuthKeyInvalid", "SessionRevoked",
                    "UserDeactivated", "UserDeactivatedBan",
                }
                if exc.__class__.__name__ in permanent_errors:
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

    async def maintenance_loop(self):
        """Expire abandoned logins and evict idle user clients."""
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            pending_users = set(self._pending_phone) | set(self._pending_qr)
            for user_id in list(pending_users):
                created = self._qr_created_at.get(user_id)
                phone = self._pending_phone.get(user_id)
                if phone:
                    created = phone.created_at
                if created and now - created > Config.AUTH_FLOW_TIMEOUT:
                    async with self._lock(user_id):
                        await self._close_pending_unlocked(user_id)

            ordinary = [
                (user_id, account) for user_id, account in self._accounts.items()
                if user_id not in self._external_accounts
            ]
            evict = {
                user_id for user_id, account in ordinary
                if now - account.last_used > Config.SESSION_IDLE_TIMEOUT
            }
            if len(ordinary) - len(evict) > Config.MAX_ACTIVE_USER_SESSIONS:
                remaining = sorted(
                    ((account.last_used, user_id) for user_id, account in ordinary
                     if user_id not in evict)
                )
                excess = len(ordinary) - len(evict) - Config.MAX_ACTIVE_USER_SESSIONS
                evict.update(user_id for _, user_id in remaining[:excess])
            for user_id in evict:
                async with self._lock(user_id):
                    await self._disconnect_unlocked(user_id)

    async def disconnect(self, user_id: int, erase: bool = True):
        async with self._lock(user_id):
            if user_id in self._external_accounts:
                raise RuntimeError("The server administrator session is managed from Linux")
            await self._disconnect_unlocked(user_id)
            if erase:
                self.db.delete_telegram_credential(user_id)

    async def cancel_pending(self, user_id: int):
        async with self._lock(user_id):
            await self._close_pending_unlocked(user_id)

    async def _disconnect_unlocked(self, user_id: int):
        await self._close_pending_unlocked(user_id)
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
        for user_id, pending in list(self._pending_phone.items()):
            if getattr(pending.client, "is_connected", False):
                await pending.client.disconnect()
            self._pending_phone.pop(user_id, None)

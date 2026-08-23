import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import session_manager
    from security import SecretBox
except ModuleNotFoundError as exc:
    if exc.name in {"pyrogram", "sqlalchemy"}:
        raise unittest.SkipTest("Runtime dependencies are installed by requirements.txt and exercised in CI")
    raise


class FakeDatabase:
    def __init__(self):
        self.saved = None

    def save_telegram_credential(self, user_id, **kwargs):
        self.saved = (user_id, kwargs)

    def get_telegram_credential(self, _user_id):
        return None

    def delete_telegram_credential(self, _user_id):
        return True


class SessionPasswordNeeded(Exception):
    pass


class FakeClient:
    is_connected = False

    def __init__(self, *_args, **_kwargs):
        self.is_connected = False

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def stop(self):
        self.is_connected = False

    async def send_code(self, _phone):
        return SimpleNamespace(phone_code_hash="phone-hash")

    async def sign_in(self, _phone, _phone_hash, code):
        if code == "22222":
            raise SessionPasswordNeeded()
        return self._user()

    async def check_password(self, password):
        if password != "correct horse":
            raise ValueError("bad password")
        return self._user()

    async def initialize(self):
        return None

    async def export_session_string(self):
        return "exported-session"

    @staticmethod
    def _user():
        return SimpleNamespace(
            id=77, username="tester", first_name="Test", last_name="User"
        )


class PhoneLoginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = FakeDatabase()
        self.box = SecretBox("MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
        self.client_patch = patch.object(session_manager, "Client", FakeClient)
        self.client_patch.start()
        self.manager = session_manager.UserSessionManager(self.database, self.box)

    def tearDown(self):
        self.client_patch.stop()

    async def test_phone_code_creates_encrypted_stored_session(self):
        await self.manager.begin_phone(
            1001, 12345, "0123456789abcdef", "+989123456789"
        )
        account = await self.manager.submit_phone_code(1001, "12345")

        self.assertEqual(account.telegram_user_id, 77)
        self.assertEqual(self.database.saved[0], 1001)
        encrypted = self.database.saved[1]["session_encrypted"]
        self.assertNotIn("exported-session", encrypted)
        self.assertEqual(self.box.decrypt(encrypted), "exported-session")

    async def test_two_factor_continues_same_pending_client(self):
        await self.manager.begin_phone(
            1002, 12345, "0123456789abcdef", "+989123456789"
        )
        with self.assertRaises(SessionPasswordNeeded):
            await self.manager.submit_phone_code(1002, "22222")

        account = await self.manager.submit_password(1002, "correct horse")
        self.assertEqual(account.username, "tester")
        self.assertNotIn(1002, self.manager._pending_phone)

    def test_authorization_attempts_are_rate_limited(self):
        with patch.object(session_manager.Config, "AUTH_ATTEMPTS_PER_HOUR", 2):
            self.manager._register_auth_attempt(1003)
            self.manager._register_auth_attempt(1003)
            with self.assertRaises(RuntimeError):
                self.manager._register_auth_attempt(1003)

    def test_touch_refreshes_active_account(self):
        account = session_manager.ConnectedAccount(
            FakeClient(), 77, "tester", "Test User", 0.0
        )
        self.manager._accounts[1004] = account
        self.manager.touch(1004)
        self.assertGreater(account.last_used, 0.0)

    async def test_capacity_evicts_least_recently_used_client(self):
        old_client = FakeClient()
        old_client.is_connected = True
        self.manager._accounts[1005] = session_manager.ConnectedAccount(
            old_client, 77, "old", "Old User", 1.0
        )
        with patch.object(session_manager.Config, "MAX_ACTIVE_USER_SESSIONS", 1):
            await self.manager._ensure_capacity(1006)
        self.assertNotIn(1005, self.manager._accounts)
        self.assertFalse(old_client.is_connected)


if __name__ == "__main__":
    unittest.main()

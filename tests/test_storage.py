import os
import tempfile
import unittest

from cryptography.fernet import Fernet

from config import Config
from security import SecretBox

try:
    from database import Database
except ModuleNotFoundError as exc:
    if exc.name == "sqlalchemy":
        raise unittest.SkipTest("SQLAlchemy is installed by requirements.txt and exercised in CI")
    raise


class MultiUserStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_data_dir = Config.DATA_DIR
        self.old_database_url = Config.DATABASE_URL
        database_path = os.path.join(self.temp_dir.name, "test.db").replace("\\", "/")
        Config.DATA_DIR = self.temp_dir.name
        Config.DATABASE_URL = f"sqlite:///{database_path}"
        self.db = Database()

    def tearDown(self):
        self.db.engine.dispose()
        Config.DATA_DIR = self.old_data_dir
        Config.DATABASE_URL = self.old_database_url
        self.temp_dir.cleanup()

    def test_profiles_and_conversation_states_are_isolated(self):
        first = 9_000_000_001
        second = 9_000_000_002
        self.db.update_user_profile(first, language="fa", onboarding_complete=True)
        self.db.set_conversation_state(first, "download_single", "encrypted-one")
        self.db.set_conversation_state(second, "setup_api_id", "encrypted-two")

        self.assertEqual(self.db.get_user_profile(first).language, "fa")
        self.assertEqual(self.db.get_user_profile(second).language, "en")
        self.assertEqual(self.db.get_conversation_state(first).payload, "encrypted-one")
        self.assertEqual(self.db.get_conversation_state(second).payload, "encrypted-two")

        self.db.clear_conversation_state(first)
        self.assertIsNone(self.db.get_conversation_state(first))
        self.assertIsNotNone(self.db.get_conversation_state(second))

    def test_credentials_are_stored_per_user_and_can_be_erased(self):
        box = SecretBox(Fernet.generate_key().decode())
        encrypted_session = box.encrypt("session-secret-value")
        self.db.save_telegram_credential(
            9_000_000_003,
            api_id_encrypted=box.encrypt("12345"),
            api_hash_encrypted=box.encrypt("hash-value"),
            session_encrypted=encrypted_session,
            status="active",
        )

        stored = self.db.get_telegram_credential(9_000_000_003)
        self.assertNotIn("session-secret-value", stored.session_encrypted)
        self.assertEqual(box.decrypt(stored.session_encrypted), "session-secret-value")
        self.assertTrue(self.db.delete_telegram_credential(9_000_000_003))
        self.assertIsNone(self.db.get_telegram_credential(9_000_000_003))

    def test_bulk_resume_and_admin_outbox_are_durable(self):
        user_id = 9_000_000_004
        job_id = "job:test"
        self.db.create_download_job(job_id, user_id, "@example")
        self.db.mark_job_item_complete(job_id, 10)
        self.db.mark_job_item_complete(job_id, 11)
        self.assertEqual(self.db.get_completed_job_items(job_id), {10, 11})

        self.db.enqueue_admin_delivery(
            "delivery:test", user_id, 123, None, "text", "mirror"
        )
        pending = self.db.get_pending_admin_deliveries()
        self.assertEqual([item.id for item in pending], ["delivery:test"])
        self.db.update_admin_delivery("delivery:test", attempts=1, status="sent")
        self.assertEqual(self.db.get_pending_admin_deliveries(), [])

        self.db.clear_user_download_state(user_id)
        self.assertEqual(self.db.get_completed_job_items(job_id), set())


if __name__ == "__main__":
    unittest.main()

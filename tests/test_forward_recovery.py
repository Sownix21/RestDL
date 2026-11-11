import unittest
import tempfile
from pathlib import Path
from pyrogram.storage.file_storage import FileStorage
from types import SimpleNamespace
from unittest.mock import AsyncMock

from helpers.forward import probe_destination, resolve_destination


class PeerIdInvalid(Exception):
    pass


class ForwardRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_flushes_peer_cache_before_success(self):
        probe = SimpleNamespace(delete=AsyncMock())
        storage = SimpleNamespace(save=AsyncMock())
        bot = SimpleNamespace(send_message=AsyncMock(return_value=probe), storage=storage)
        await probe_destination(bot, -100123, "test")
        storage.save.assert_awaited_once()

    async def test_setup_recovers_unknown_public_destination(self):
        chat = SimpleNamespace(id=-100123, username="destination")
        bot = SimpleNamespace(get_chat=AsyncMock(side_effect=[PeerIdInvalid(), chat]))
        source = SimpleNamespace(get_chat=AsyncMock(return_value=chat))
        self.assertIs(await resolve_destination(bot, "-100123", source), chat)
        self.assertEqual(bot.get_chat.await_args.args, ("@destination",))

    async def test_private_channel_peer_survives_session_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FileStorage("bot_test", Path(directory))
            await store.open()
            await store.update_peers([(-100123, 98765, "channel", None, None)])
            await store.save()
            await store.close()
            restored = FileStorage("bot_test", Path(directory))
            await restored.open()
            try:
                peer = await restored.get_peer_by_id(-100123)
                self.assertEqual(peer.access_hash, 98765)
            finally:
                await restored.close()

    async def test_deletion_failure_does_not_reject_working_destination(self):
        probe = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("cannot delete")))
        bot = SimpleNamespace(send_message=AsyncMock(return_value=probe))
        self.assertIs(await probe_destination(bot, "-100123", "test"), probe)
        bot.send_message.assert_awaited_once_with(-100123, "test")

    async def test_public_peer_is_recovered_using_bots_own_account(self):
        chat = SimpleNamespace(id=-100123, username="destination")
        source = SimpleNamespace(get_chat=AsyncMock(return_value=chat))
        probe = SimpleNamespace(delete=AsyncMock())
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=[PeerIdInvalid(), probe]),
            get_chat=AsyncMock(return_value=chat),
        )
        self.assertIs(await probe_destination(bot, chat.id, "test", source), probe)
        bot.get_chat.assert_awaited_once_with("@destination")

    async def test_no_retry_or_destination_change_on_permission_failure(self):
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("forbidden")))
        with self.assertRaises(RuntimeError):
            await probe_destination(bot, -100123, "test")
        self.assertEqual(bot.send_message.await_count, 1)

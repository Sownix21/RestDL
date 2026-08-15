import unittest
from types import SimpleNamespace

from helpers.chats import resolve_chat
from helpers.files import get_readable_file_size, sanitize_filename
from i18n import TRANSLATIONS, tr
from helpers.msg import (
    extract_urls,
    format_message_link,
    getChatMsgID,
    getStoryChatMsgID,
    is_valid_telegram_url,
    parse_chat_id_direct,
    parse_chat_identifier,
)


class TelegramIdentifierTests(unittest.TestCase):
    def test_private_post_link_adds_internal_prefix(self):
        self.assertEqual(getChatMsgID("https://t.me/c/1719871015/123"), (-1001719871015, 123))

    def test_private_topic_link_uses_last_message_id(self):
        self.assertEqual(getChatMsgID("https://t.me/c/1719871015/9/123?single"), (-1001719871015, 123))

    def test_public_and_preview_links(self):
        self.assertEqual(getChatMsgID("https://telegram.me/example/55"), ("@example", 55))
        self.assertEqual(getChatMsgID("https://t.me/s/example/55"), ("@example", 55))

    def test_story_does_not_parse_as_post(self):
        self.assertEqual(getStoryChatMsgID("https://t.me/example/s/7"), ("@example", 7))
        with self.assertRaises(ValueError):
            getChatMsgID("https://t.me/example/s/7")

    def test_positive_numeric_id_is_not_forced_to_channel(self):
        self.assertEqual(parse_chat_id_direct("123456789"), (123456789, None))

    def test_chat_only_urls_and_resolve_links(self):
        self.assertEqual(parse_chat_identifier("https://t.me/example"), "@example")
        self.assertEqual(parse_chat_identifier("tg://resolve?domain=example"), "@example")

    def test_private_link_format_strips_internal_prefix(self):
        self.assertEqual(format_message_link(-1001719871015, 123), "https://t.me/c/1719871015/123")

    def test_host_validation_and_extraction(self):
        self.assertTrue(is_valid_telegram_url("https://telegram.dog/example/1"))
        self.assertFalse(is_valid_telegram_url("https://example.com/t.me/example/1"))
        self.assertEqual(extract_urls("See https://t.me/example/1)."), ["https://t.me/example/1"])

    def test_file_size_formatting_uses_binary_units(self):
        self.assertEqual(get_readable_file_size(1024 * 1024), "1.00 MB")

    def test_telegram_filename_cannot_escape_download_directory(self):
        self.assertEqual(sanitize_filename("../../secret.txt"), "secret.txt")
        self.assertEqual(sanitize_filename("bad:name?.mp4"), "bad_name_.mp4")

    def test_locales_have_identical_interface_keys(self):
        self.assertEqual(set(TRANSLATIONS["en"]), set(TRANSLATIONS["fa"]))
        self.assertIn("منوی اصلی", tr("fa", "main_title"))


class ChatResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_bare_positive_channel_id_gets_safe_fallback(self):
        expected = SimpleNamespace(id=-100123, username=None, title="Channel")

        class Client:
            async def get_chat(self, identifier):
                if identifier == -100123:
                    return expected
                raise RuntimeError("unknown peer")

            async def get_dialogs(self, limit=0):
                if False:
                    yield None

        self.assertIs(await resolve_chat(Client(), 123), expected)

    async def test_dialog_refresh_finds_uncached_member_chat(self):
        expected = SimpleNamespace(id=-456, username=None, title="Group")

        class Client:
            async def get_chat(self, identifier):
                raise RuntimeError("PEER_ID_INVALID")

            async def get_dialogs(self, limit=0):
                yield SimpleNamespace(chat=expected)

        self.assertIs(await resolve_chat(Client(), -456), expected)


if __name__ == "__main__":
    unittest.main()

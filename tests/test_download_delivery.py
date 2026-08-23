import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from config import Config
    from helpers.downloader import Downloader
    from helpers.utils import process_media_group
except ModuleNotFoundError as exc:
    if exc.name in {"pyrogram", "sqlalchemy"}:
        raise unittest.SkipTest("Runtime dependencies are installed by requirements.txt")
    raise


class AlbumDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_destination_does_not_complete_album(self):
        temporary = tempfile.TemporaryDirectory()
        old_download_dir = Config.DOWNLOAD_DIR
        Config.DOWNLOAD_DIR = temporary.name
        replies = []

        class AlbumItem:
            id = 55
            photo = SimpleNamespace(file_size=3)
            video = document = audio = animation = voice = video_note = sticker = None

            async def download(self, file_name):
                Path(file_name).write_bytes(b"abc")
                return file_name

        class SourceClient:
            async def get_media_group(self, chat_id, message_id):
                return [AlbumItem()]

        class Request:
            id = 99

            async def reply(self, text, **_kwargs):
                replies.append(text)

            async def reply_photo(self, **_kwargs):
                raise RuntimeError("destination unavailable")

        source_message = SimpleNamespace(chat=SimpleNamespace(id=-1001), id=55)
        try:
            completed = await process_media_group(
                source_message, SourceClient(), Request(), language="en"
            )
        finally:
            Config.DOWNLOAD_DIR = old_download_dir
            temporary.cleanup()

        self.assertFalse(completed)
        self.assertIn("failed: 1", replies[-1])

    async def test_clear_state_cannot_remove_another_users_resume_file(self):
        temporary = tempfile.TemporaryDirectory()
        old_data_dir = Config.DATA_DIR
        Config.DATA_DIR = temporary.name
        state_dir = Path(temporary.name) / "download_states"
        state_dir.mkdir()
        own = state_dir / "12_channel.json"
        other = state_dir / "123_channel.json"
        own.write_text("{}", encoding="utf-8")
        other.write_text("{}", encoding="utf-8")
        instance = Downloader.__new__(Downloader)
        instance.owner_user_id = 12
        instance.state = SimpleNamespace(clear=lambda: None)
        try:
            instance.clear_state()
        finally:
            Config.DATA_DIR = old_data_dir
        self.assertFalse(own.exists())
        self.assertTrue(other.exists())
        temporary.cleanup()


if __name__ == "__main__":
    unittest.main()

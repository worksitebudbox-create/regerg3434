import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from aiogram import Router
from aiogram.types import ReplyKeyboardMarkup

from .config import Settings
from .main import (
    _extract_tiktok_video_link,
    _menu_keyboard,
    build_caption,
    register_handlers,
)
from .media_processing import MediaProcessingError, normalize_video
from .storage import Database
from .tiktok import (
    TikTokError,
    Video,
    download_tiktok_video,
    normalize_profile_url,
)


class MediaProcessingTests(unittest.TestCase):
    def test_caption_removes_hashtags_from_title_and_description(self) -> None:
        video = Video(
            video_id="123456",
            url="https://www.tiktok.com/@creator/video/123456",
            title="Новий ролик #тест",
            description="Текст опису #рекомендації #дорама",
            channel="creator",
            thumbnail=None,
            tags=("recommendations",),
        )

        caption = build_caption(video, "Профіль")

        self.assertIn("Текст опису", caption)
        self.assertIn("Новий ролик", caption)
        self.assertIn(video.url, caption)
        self.assertNotIn("#", caption)
        self.assertNotIn("Хештеги", caption)

    def test_tiktok_downloader_rejects_non_tiktok_url(self) -> None:
        with self.assertRaises(TikTokError):
            download_tiktok_video(
                "https://example.com/video/123456",
                output_dir=Path("."),
                max_bytes=1024,
            )

    def test_profile_normalization_accepts_at_handle_and_url(self) -> None:
        self.assertEqual(
            normalize_profile_url("@creator.name"),
            "https://www.tiktok.com/@creator.name",
        )
        self.assertEqual(
            normalize_profile_url("https://www.tiktok.com/@creator.name"),
            "https://www.tiktok.com/@creator.name",
        )

    def test_extract_tiktok_video_link_from_plain_text(self) -> None:
        video_url = "https://www.tiktok.com/@creator/video/123456789"
        self.assertEqual(
            _extract_tiktok_video_link(f"Смотри это: {video_url}!"),
            video_url,
        )
        self.assertIsNone(_extract_tiktok_video_link("/save " + video_url))

    def test_handlers_register(self) -> None:
        settings = Settings(
            bot_token="token",
            admin_ids=frozenset({1}),
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="key",
            poll_interval_seconds=900,
            max_videos_per_profile=20,
        )

        database = Database("https://example.supabase.co", "key")
        register_handlers(Router(), database, settings)

    def test_menu_is_a_persistent_reply_keyboard(self) -> None:
        keyboard = _menu_keyboard(is_admin=True)

        self.assertIsInstance(keyboard, ReplyKeyboardMarkup)
        self.assertTrue(keyboard.is_persistent)
        self.assertTrue(
            any(
                button.text == "⬇️ Скачать видео по ссылке"
                for row in keyboard.keyboard
                for button in row
            )
        )

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "FFmpeg tools are not installed",
    )
    def test_video_normalization_outputs_h264_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.webm"
            output = Path(temp_dir) / "normalized.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=160x120:rate=10",
                    "-t",
                    "1",
                    str(source),
                ],
                check=True,
                capture_output=True,
            )

            normalize_video(source, output)
            info = json.loads(
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=format_name:stream=codec_name,codec_type",
                        "-of",
                        "json",
                        str(output),
                    ],
                    text=True,
                )
            )

            self.assertTrue(output.is_file())
            self.assertIn("mp4", info["format"]["format_name"])
            video_stream = next(
                stream for stream in info["streams"] if stream["codec_type"] == "video"
            )
            self.assertEqual(video_stream["codec_name"], "h264")


if __name__ == "__main__":
    unittest.main()

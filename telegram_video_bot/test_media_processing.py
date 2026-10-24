import io
import unittest
from pathlib import Path

from aiogram import Router
from PIL import Image

from .config import Settings
from .main import (
    _extract_tiktok_video_link,
    build_caption,
    register_handlers,
)
from .media_processing import MediaProcessingError, enhance_image
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

    def test_image_enhancement_preserves_dimensions(self) -> None:
        original = Image.new("RGB", (32, 24), color=(120, 80, 40))
        source = io.BytesIO()
        original.save(source, format="PNG")

        result = Image.open(io.BytesIO(enhance_image(source.getvalue())))

        self.assertEqual(result.size, original.size)
        self.assertEqual(result.format, "JPEG")

    def test_image_enhancement_rejects_invalid_input(self) -> None:
        with self.assertRaises(MediaProcessingError):
            enhance_image(b"not an image")

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


if __name__ == "__main__":
    unittest.main()

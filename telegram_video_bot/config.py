"""Завантаження та перевірка налаштувань бота."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    supabase_url: str
    supabase_service_role_key: str
    poll_interval_seconds: int
    max_videos_per_profile: int


def _read_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} має бути цілим числом.") from exc


def load_settings() -> Settings:
    """Читає лише змінні середовища процесу та перевіряє обов'язкові значення."""
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    raw_admin_ids = os.getenv("ADMIN_IDS", "").strip()
    supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    supabase_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    missing = [
        name
        for name, value in (
            ("BOT_TOKEN", bot_token),
            ("ADMIN_IDS", raw_admin_ids),
            ("SUPABASE_URL", supabase_url),
            ("SUPABASE_SERVICE_ROLE_KEY", supabase_service_role_key),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Не задано обов'язкові параметри: "
            + ", ".join(missing)
            + ". Додайте їх у Variables цього Railway-сервісу "
            "або задайте в середовищі процесу."
        )

    parsed_url = urlparse(supabase_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("SUPABASE_URL має бути коректною HTTP(S)-адресою.")

    try:
        admin_ids = frozenset(
            int(value.strip())
            for value in raw_admin_ids.split(",")
            if value.strip()
        )
    except ValueError as exc:
        raise ValueError("ADMIN_IDS має містити числові Telegram ID через кому.") from exc

    if not admin_ids:
        raise ValueError("ADMIN_IDS має містити принаймні один Telegram ID.")

    poll_interval = _read_int("POLL_INTERVAL_SECONDS", 900)
    if poll_interval < 60:
        raise ValueError("POLL_INTERVAL_SECONDS не може бути меншим за 60.")

    max_videos = _read_int("MAX_VIDEOS_PER_PROFILE", 20)
    if not 1 <= max_videos <= 50:
        raise ValueError("MAX_VIDEOS_PER_PROFILE має бути від 1 до 50.")

    return Settings(
        bot_token=bot_token,
        admin_ids=admin_ids,
        supabase_url=supabase_url,
        supabase_service_role_key=supabase_service_role_key,
        poll_interval_seconds=poll_interval,
        max_videos_per_profile=max_videos,
    )

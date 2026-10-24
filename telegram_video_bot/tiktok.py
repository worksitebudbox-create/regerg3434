"""Отримання публічних метаданих TikTok через yt-dlp без завантаження відео."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp


class TikTokError(RuntimeError):
    """Помилка читання публічного профілю або відео TikTok."""


@dataclass(frozen=True, slots=True)
class Video:
    video_id: str
    url: str
    title: str
    description: str
    channel: str
    thumbnail: str | None
    tags: tuple[str, ...]

    def storage_record(self) -> dict[str, str]:
        return {
            "video_id": self.video_id,
            "url": self.url,
            "title": self.title,
        }


_PROFILE_PATH = re.compile(r"^/@([A-Za-z0-9._-]+)/?$")
_VIDEO_PATH = re.compile(r"^/@([A-Za-z0-9._-]+)/video/(\d+)/?$")
_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}


def normalize_profile_url(raw_url: str) -> str:
    """Перевіряє URL або @handle профілю й повертає канонічне посилання."""
    value = raw_url.strip()
    handle = value.removeprefix("@")
    if re.fullmatch(r"[A-Za-z0-9._-]+", handle):
        return f"https://www.tiktok.com/@{handle}"

    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()

    if parsed.scheme not in {"http", "https"} or host not in _TIKTOK_HOSTS:
        raise ValueError(
            "Потрібне посилання на профіль TikTok, наприклад "
            "https://www.tiktok.com/@handle."
        )

    match = _PROFILE_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError(
            "Вкажіть посилання на профіль виду https://www.tiktok.com/@handle, "
            "а не посилання на окреме відео чи коротке посилання."
        )

    return f"https://www.tiktok.com/@{match.group(1)}"


def _ydl_options(*, flat: bool, limit: int | None = None) -> dict:
    options: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "ignoreerrors": False,
    }
    if flat:
        options["extract_flat"] = "in_playlist"
        if limit is not None:
            options["playlistend"] = limit
    return options


def _best_thumbnail(info: dict) -> str | None:
    thumbnails = info.get("thumbnails") or []
    if thumbnails:
        valid = [
            image
            for image in thumbnails
            if isinstance(image, dict) and image.get("url")
        ]
        if valid:
            best = max(valid, key=lambda image: image.get("width") or 0)
            return str(best["url"])
    thumbnail = info.get("thumbnail")
    return str(thumbnail) if thumbnail else None


def _video_url(info: dict, video_id: str, fallback_handle: str) -> str:
    """Використовує лише URL з домену TikTok або формує канонічне посилання."""
    for candidate in (info.get("webpage_url"), info.get("url")):
        if not candidate:
            continue
        parsed = urlsplit(str(candidate))
        host = (parsed.hostname or "").lower()
        match = _VIDEO_PATH.fullmatch(parsed.path)
        if (
            host in _TIKTOK_HOSTS
            and match
            and match.group(2) == video_id
        ):
            return f"https://www.tiktok.com/@{match.group(1)}/video/{video_id}"

    # TikTok metadata exposes the public handle as uploader/creator. Validate it
    # before using it to build a URL; the profile handle is the final fallback.
    for candidate in (
        info.get("uploader"),
        info.get("creator"),
        fallback_handle,
    ):
        handle = str(candidate or "").strip().lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9._-]+", handle):
            return f"https://www.tiktok.com/@{handle}/video/{video_id}"

    return f"https://www.tiktok.com/@_/video/{video_id}"


def _make_video(info: dict, *, fallback_handle: str = "") -> Video | None:
    raw_video_id = info.get("id")
    if not raw_video_id:
        return None
    video_id = str(raw_video_id)
    # TikTok video IDs are numeric. Reject malformed IDs before building links.
    if not re.fullmatch(r"\d+", video_id):
        return None

    raw_tags = info.get("tags") or []
    tags = tuple(str(tag) for tag in raw_tags if tag)

    return Video(
        video_id=video_id,
        url=_video_url(info, video_id, fallback_handle),
        title=str(info.get("title") or "Без назви"),
        description=str(info.get("description") or ""),
        channel=str(
            info.get("channel")
            or info.get("uploader")
            or info.get("creator")
            or (f"@{fallback_handle}" if fallback_handle else "TikTok")
        ),
        thumbnail=_best_thumbnail(info),
        tags=tags,
    )


def fetch_latest_videos(url: str, limit: int) -> tuple[str, list[Video]]:
    """Повертає назву профілю та останні відео; викликати через asyncio.to_thread."""
    handle_match = _PROFILE_PATH.fullmatch(urlsplit(url).path)
    fallback_handle = handle_match.group(1) if handle_match else ""

    try:
        with yt_dlp.YoutubeDL(_ydl_options(flat=True, limit=limit)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise TikTokError(f"Не вдалося прочитати профіль TikTok: {exc}") from exc

    if not info:
        raise TikTokError("TikTok не повернув дані профілю.")

    profile_name = str(
        info.get("title")
        or info.get("uploader")
        or (f"@{fallback_handle}" if fallback_handle else url)
    )
    entries = info.get("entries") or []
    videos = [
        video
        for entry in entries
        if isinstance(entry, dict)
        if (video := _make_video(entry, fallback_handle=fallback_handle)) is not None
    ]
    return profile_name, videos


def fetch_video_details(video_url: str, fallback_handle: str = "") -> Video:
    """Зчитує опис, теги та прев'ю відео TikTok без його завантаження."""
    parsed = urlsplit(video_url)
    host = (parsed.hostname or "").lower()
    match = _VIDEO_PATH.fullmatch(parsed.path)
    if host not in _TIKTOK_HOSTS or not match:
        raise TikTokError("Неприпустиме посилання на відео TikTok.")

    canonical_url = (
        f"https://www.tiktok.com/@{match.group(1)}/video/{match.group(2)}"
    )
    try:
        with yt_dlp.YoutubeDL(_ydl_options(flat=False)) as ydl:
            info = ydl.extract_info(canonical_url, download=False)
    except Exception as exc:
        raise TikTokError(
            f"Не вдалося прочитати відео TikTok {match.group(2)}: {exc}"
        ) from exc

    if not info:
        raise TikTokError(f"TikTok не повернув дані відео {match.group(2)}.")

    video = _make_video(info, fallback_handle=fallback_handle)
    if video is None:
        raise TikTokError(f"У відео TikTok {match.group(2)} немає коректного ID.")
    return video


def download_tiktok_video(
    video_url: str,
    output_dir: Path,
    max_bytes: int,
) -> Path:
    """Download one public TikTok video to a bounded temporary directory."""
    parsed = urlsplit(video_url)
    host = (parsed.hostname or "").lower()
    match = _VIDEO_PATH.fullmatch(parsed.path)
    if host not in _TIKTOK_HOSTS or not match:
        raise TikTokError("Неприпустиме посилання на відео TikTok.")

    canonical_url = (
        f"https://www.tiktok.com/@{match.group(1)}/video/{match.group(2)}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    options = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "max_filesize": max_bytes,
        "outtmpl": str(output_dir / "video.%(ext)s"),
    }

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(canonical_url, download=True)
            if not info:
                raise TikTokError("TikTok не повернув відеофайл.")
            candidate = Path(
                str(info.get("filepath") or ydl.prepare_filename(info))
            )
            if not candidate.is_file():
                candidate = candidate.with_suffix(".mp4")
            if not candidate.is_file():
                files = list(output_dir.glob("video.*"))
                candidate = next(
                    (
                        path
                        for path in files
                        if path.suffix.lower() in {".mp4", ".m4v", ".mov", ".webm"}
                    ),
                    Path(),
                )
    except TikTokError:
        raise
    except Exception as exc:
        raise TikTokError(f"Не вдалося завантажити відео TikTok: {exc}") from exc

    if not candidate.is_file():
        raise TikTokError("TikTok не створив відеофайл.")
    if candidate.stat().st_size > max_bytes:
        candidate.unlink(missing_ok=True)
        raise TikTokError("Відео завелике для надсилання через Telegram.")
    return candidate

"""Video filters and MP4 normalization using FFmpeg."""

from __future__ import annotations

import subprocess
from pathlib import Path

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 45 * 1024 * 1024
VIDEO_PROCESSING_TIMEOUT_SECONDS = 180


class MediaProcessingError(RuntimeError):
    """A media file could not be processed within the bot's limits."""


def enhance_video(source: Path, destination: Path) -> None:
    """Apply light filters and encode a video as H.264/AAC MP4."""
    _convert_video(source, destination, apply_filters=True)


def normalize_video(source: Path, destination: Path) -> None:
    """Transcode a video to compatible H.264/AAC MP4 without visual filters."""
    _convert_video(source, destination, apply_filters=False)


def _convert_video(source: Path, destination: Path, *, apply_filters: bool) -> None:
    filters = "hqdn3d=1.5:1.5:6:6,unsharp=5:5:0.5:5:5:0.0"
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        *(
            ["-vf", filters]
            if apply_filters
            else []
        ),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-fs", str(MAX_OUTPUT_BYTES - 1024 * 1024),
        "-f", "mp4",
        str(destination),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=VIDEO_PROCESSING_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise MediaProcessingError("FFmpeg is not installed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaProcessingError("Video processing timed out.") from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or "").strip()
        raise MediaProcessingError(
            f"FFmpeg could not process the video: {details[-500:]}"
        ) from exc

    if not destination.is_file() or destination.stat().st_size == 0:
        raise MediaProcessingError("FFmpeg produced no output.")
    if destination.stat().st_size >= MAX_OUTPUT_BYTES - 2 * 1024 * 1024:
        raise MediaProcessingError("Processed video reached the Telegram file-size limit.")

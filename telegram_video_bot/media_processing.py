"""Free, non-AI filters for photos and videos."""

from __future__ import annotations

import io
import subprocess
import warnings
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 45 * 1024 * 1024
VIDEO_PROCESSING_TIMEOUT_SECONDS = 180


class MediaProcessingError(RuntimeError):
    """A media file could not be processed within the bot's limits."""


def enhance_image(source: bytes) -> bytes:
    """Apply mild color, contrast, and sharpening adjustments without upscaling."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(source)) as image:
                image = ImageOps.exif_transpose(image)
                image = ImageEnhance.Color(image).enhance(1.08)
                image = ImageEnhance.Contrast(image).enhance(1.05)
                image = ImageEnhance.Sharpness(image).enhance(1.15)
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                if image.mode == "L":
                    image = image.convert("RGB")

                output = io.BytesIO()
                image.save(output, format="JPEG", quality=94, optimize=True)
                return output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise MediaProcessingError("Unsupported or invalid image.") from exc


def enhance_video(source: Path, destination: Path) -> None:
    """Denoise and lightly sharpen a video while preserving its source resolution."""
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        "hqdn3d=1.5:1.5:6:6,unsharp=5:5:0.5:5:5:0.0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-fs",
        str(MAX_OUTPUT_BYTES - 1024 * 1024),
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

"""Асинхронне збереження профілів і відео через Supabase REST API."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class SupabaseError(RuntimeError):
    """Помилка запиту до Supabase."""


class Database:
    def __init__(self, url: str, service_role_key: str) -> None:
        self.url = url.rstrip("/")
        self.service_role_key = service_role_key
        self._initialized = False

    async def initialize(self) -> None:
        await asyncio.to_thread(
            self._request,
            "GET",
            "profiles",
            params={"select": "id", "limit": "1"},
        )
        self._initialized = True

    async def close(self) -> None:
        self._initialized = False

    def _request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        params: dict[str, str] | None = None,
        prefer: str | None = None,
    ) -> Any:
        url = f"{self.url}/rest/v1/{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer

        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read()
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace").strip()
            raise SupabaseError(
                f"Supabase API повернув HTTP {exc.code}: {details or exc.reason}"
            ) from exc
        except URLError as exc:
            raise SupabaseError(
                f"Не вдалося підключитися до Supabase: {exc.reason}"
            ) from exc

        if not response_body:
            return None
        try:
            return json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise SupabaseError(
                f"Supabase повернув некоректну JSON-відповідь для {path}."
            ) from exc

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("З'єднання з Supabase ще не ініціалізовано.")

    async def add_profile(
        self,
        url: str,
        display_name: str,
        baseline_videos: list[dict[str, str]],
    ) -> int | None:
        """Атомарно додає профіль і фіксує початкову базову лінію."""
        self._ensure_initialized()
        result = await asyncio.to_thread(
            self._request,
            "POST",
            "rpc/add_tiktok_profile",
            {
                "p_url": url,
                "p_display_name": display_name,
                "p_baseline_videos": baseline_videos,
            },
        )
        if result is None:
            return None
        if isinstance(result, int) and not isinstance(result, bool):
            return result
        if isinstance(result, list) and len(result) == 1:
            profile_id = result[0]
            if isinstance(profile_id, int) and not isinstance(profile_id, bool):
                return profile_id
        raise SupabaseError("Supabase повернув неочікуваний ID нового профілю.")

    async def remove_profile(self, profile_id: int) -> bool:
        self._ensure_initialized()
        result = await asyncio.to_thread(
            self._request,
            "DELETE",
            "profiles",
            params={
                "id": f"eq.{profile_id}",
                "select": "id",
            },
            prefer="return=representation",
        )
        if not isinstance(result, list):
            raise SupabaseError("Supabase повернув неочікувану відповідь на видалення.")
        return bool(result)

    async def list_profiles(self) -> list[dict[str, Any]]:
        self._ensure_initialized()
        result = await asyncio.to_thread(
            self._request,
            "GET",
            "profiles",
            params={
                "select": "id,platform,url,display_name,created_at",
                "platform": "eq.tiktok",
                "order": "id.asc",
            },
        )
        if not isinstance(result, list):
            raise SupabaseError("Supabase повернув неочікуваний список профілів.")
        return result

    async def get_known_video_ids(
        self, profile_id: int, video_ids: list[str]
    ) -> set[str]:
        self._ensure_initialized()
        if not video_ids:
            return set()

        video_filter = "in.(" + ",".join(
            json.dumps(video_id) for video_id in video_ids
        ) + ")"
        result = await asyncio.to_thread(
            self._request,
            "GET",
            "videos",
            params={
                "select": "video_id",
                "profile_id": f"eq.{profile_id}",
                "video_id": video_filter,
            },
        )
        if not isinstance(result, list):
            raise SupabaseError("Supabase повернув неочікувану історію відео.")
        return {str(row["video_id"]) for row in result}

    async def mark_video_notified(
        self, profile_id: int, video: dict[str, str]
    ) -> None:
        """Записує ID лише після успішного надсилання сповіщень."""
        self._ensure_initialized()
        await asyncio.to_thread(
            self._request,
            "POST",
            "videos",
            {
                "profile_id": profile_id,
                "video_id": video["video_id"],
                "url": video["url"],
                "title": video["title"],
                "state": "notified",
                "notified_at": datetime.now(timezone.utc).isoformat(),
            },
            params={"on_conflict": "profile_id,video_id"},
            prefer="resolution=merge-duplicates,return=minimal",
        )

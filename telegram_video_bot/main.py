"""Telegram-бот: адмін-команди та фоновий моніторинг профілів TikTok."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import tempfile
from collections.abc import Iterable
from html import escape
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .config import Settings, load_settings
from .media_processing import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    MediaProcessingError,
    enhance_image,
    enhance_video,
)
from .storage import Database, SupabaseError
from .tiktok import (
    TikTokError,
    Video,
    download_tiktok_video,
    fetch_latest_videos,
    fetch_video_details,
    normalize_profile_url,
)

logger = logging.getLogger("tiktok_monitor")
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_PHOTO_LIMIT_BYTES = 10 * 1024 * 1024
HEARTBEAT_INTERVAL_SECONDS = 300


async def _health_check(request: web.Request) -> web.Response:
    return web.Response(
        text="ok\n",
        headers={"X-Health-Check": request.path},
    )


def _create_health_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _health_check)
    app.router.add_get("/health", _health_check)
    return app


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _truncate_utf16(value: str, max_units: int) -> str:
    if max_units <= 0:
        return ""
    result: list[str] = []
    used = 0
    for character in value:
        units = _utf16_length(character)
        if used + units > max_units:
            break
        result.append(character)
        used += units
    text = "".join(result)
    if text != value and max_units >= 2:
        while text and _utf16_length(text + "…") > max_units:
            text = text[:-1]
        text += "…"
    return text


def _remove_hashtags(value: str) -> str:
    cleaned = re.sub(r"(?<!\w)#[\w]+", "", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


_TIKTOK_VIDEO_LINK = re.compile(
    r"https?://(?:www\.)?(?:m\.)?tiktok\.com/"
    r"@[A-Za-z0-9._-]+/video/\d+/?(?:\?[^\s]*)?",
    flags=re.IGNORECASE,
)


def _extract_tiktok_video_link(text: str) -> str | None:
    if text.lstrip().startswith("/"):
        return None
    match = _TIKTOK_VIDEO_LINK.search(text)
    if match is None:
        return None
    return match.group(0).rstrip(".,!?;:)]}")


def build_caption(video: Video, profile_name: str) -> str:
    """Формує підпис без хештегів і з посиланням у межах 1024 символів."""
    profile = _truncate_utf16(_remove_hashtags(profile_name) or "TikTok", 120)
    title = _truncate_utf16(_remove_hashtags(video.title) or "Без назви", 240)
    link_line = f"Посилання: {video.url}"
    prefix = f"Нове відео\nПрофіль: {profile}\nНазва: {title}\n\nОпис: "
    suffix = f"\n\n{link_line}"
    description = _remove_hashtags(video.description) or "Опис відсутній."

    available = TELEGRAM_CAPTION_LIMIT - _utf16_length(prefix) - _utf16_length(suffix)
    if available < 1:
        # Заголовок і URL зазвичай короткі; це запасний шлях
        # на випадок незвично довгих метаданих.
        profile = _truncate_utf16(profile, 60)
        title = _truncate_utf16(title, 120)
        prefix = f"Нове відео\nПрофіль: {profile}\nНазва: {title}\n\nОпис: "
        available = max(
            1,
            TELEGRAM_CAPTION_LIMIT - _utf16_length(prefix) - _utf16_length(suffix),
        )

    description = _truncate_utf16(description, available)
    return prefix + description + suffix


def _admin_only(message: Message, settings: Settings) -> bool:
    return message.from_user is not None and message.from_user.id in settings.admin_ids


async def _require_admin(message: Message, settings: Settings) -> bool:
    if _admin_only(message, settings):
        return True
    await message.answer("Ця команда доступна лише адміністраторам бота.")
    return False


def _format_profile(profile: dict) -> str:
    return (
        f"#{profile['id']} · {profile['display_name']}\n"
        f"{profile['url']}"
    )


def _split_messages(lines: Iterable[str], limit: int = 3800) -> list[str]:
    messages: list[str] = []
    current = ""
    for line in lines:
        if current and len(current) + len(line) + 1 > limit:
            messages.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        messages.append(current)
    return messages


def _menu_keyboard(section: str, is_admin: bool) -> InlineKeyboardMarkup:
    if section == "home":
        rows = []
        if is_admin:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="➕ Додати профіль",
                        callback_data="menu:add",
                    ),
                    InlineKeyboardButton(
                        text="📋 Мої профілі",
                        callback_data="menu:profiles",
                    ),
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text="⬇️ Скачать видео по ссылке",
                        callback_data="menu:save",
                    )
                ]
            )
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="🆔 Мій chat ID",
                        callback_data="menu:chatid",
                    ),
                    InlineKeyboardButton(
                        text="ℹ️ Довідка",
                        callback_data="menu:help",
                    ),
                ],
            ]
        )
    else:
        rows = []
        if section == "profiles" and is_admin:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="➕ Додати профіль",
                        callback_data="menu:add",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🏠 Головне меню",
                    callback_data="menu:home",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _profiles_keyboard(profiles: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"🗑 #{profile['id']}",
                callback_data=f"profile:remove:{profile['id']}",
            )
        ]
        for profile in profiles[:12]
    ]
    if profiles:
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ Додати профіль",
                    callback_data="menu:add",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🏠 Головне меню",
                callback_data="menu:home",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _main_menu_text(is_admin: bool) -> str:
    access = "🛡 Доступ адміністратора" if is_admin else "👋 Відкритий доступ"
    return (
        "<b>✨ RADAR MEDIA</b>\n"
        "<i>Моніторинг нових відео TikTok</i>\n\n"
        "Відстежуйте свіжі публікації відкритих профілів "
        "і отримуйте сповіщення просто в Telegram.\n\n"
        f"🟢 Сервіс активний\n{access}\n\n"
        "<i>Оберіть дію кнопками нижче. Керування — через меню.</i>"
    )


class ProfileAddStates(StatesGroup):
    waiting_for_profile = State()


class SaveVideoStates(StatesGroup):
    waiting_for_url = State()


def register_handlers(
    router: Router,
    database: Database,
    settings: Settings,
) -> None:
    async def show_main_menu(message: Message) -> None:
        is_admin = _admin_only(message, settings)
        await message.answer(
            _main_menu_text(is_admin),
            reply_markup=_menu_keyboard("home", is_admin),
            parse_mode="HTML",
        )

    async def add_profile_from_input(message: Message, raw_value: str) -> bool:
        if not await _require_admin(message, settings):
            return False
        try:
            profile_url = normalize_profile_url(raw_value)
        except ValueError as exc:
            await message.answer(f"{exc}\nНадішліть @нік або посилання ще раз.")
            return False

        await message.answer("Перевіряю профіль TikTok…")
        try:
            channel_name, existing_videos = await asyncio.to_thread(
                fetch_latest_videos,
                profile_url,
                settings.max_videos_per_profile,
            )
        except TikTokError as exc:
            logger.warning("Could not add TikTok profile %s: %s", profile_url, exc)
            await message.answer(
                "Не вдалося перевірити профіль. Переконайтеся, що він відкритий "
                "і посилання або @нік правильні, потім спробуйте ще раз."
            )
            return False

        profile_id = await database.add_profile(
            profile_url,
            channel_name,
            [video.storage_record() for video in existing_videos],
        )
        if profile_id is None:
            await message.answer("Цей профіль уже є у списку.")
            return True

        await message.answer(
            f"✅ Профіль «{channel_name}» додано.\n"
            f"Поточні відео ({len(existing_videos)}) позначені як уже відомі. "
            "Сповіщення надходитимуть про нові публікації."
        )
        return True

    @router.message(CommandStart())
    async def start_handler(message: Message) -> None:
        await show_main_menu(message)

    @router.message(Command("menu"))
    async def menu_handler(message: Message) -> None:
        await show_main_menu(message)

    @router.callback_query(F.data.startswith("menu:"))
    async def menu_callback_handler(
        callback: CallbackQuery,
        state: FSMContext,
    ) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer("Це меню вже недоступне.", show_alert=True)
            return

        is_admin = callback.from_user.id in settings.admin_ids
        section = (callback.data or "").removeprefix("menu:")
        if section != "add":
            if section != "save":
                await state.clear()
        if section == "home":
            text = _main_menu_text(is_admin)
        elif section == "cancel_add":
            text = _main_menu_text(is_admin)
            section = "home"
        elif section == "profiles":
            if not is_admin:
                await callback.answer(
                    "Список профілів доступний лише адміністраторам.",
                    show_alert=True,
                )
                return

            profiles = await database.list_profiles()
            if profiles:
                entries = [
                    f"• <b>#{profile['id']}</b> "
                    f"<a href=\"{escape(str(profile['url']), quote=True)}\">"
                    f"{escape(_truncate_utf16(str(profile['display_name']), 80))}"
                    "</a>"
                    for profile in profiles[:12]
                ]
                text = (
                    "<b>📋 ВІДСТЕЖУВАНІ ПРОФІЛІ</b>\n\n"
                    + "\n".join(entries)
                )
                if len(profiles) > 12:
                    text += (
                        f"\n\n<i>Показано 12 із {len(profiles)}. "
                        "Видалення та додавання доступні кнопками.</i>"
                    )
            else:
                text = (
                    "<b>📋 ВІДСТЕЖУВАНІ ПРОФІЛІ</b>\n\n"
                    "Поки що профілів немає.\n"
                    "Додайте перший профіль за кнопкою нижче."
                )
        elif section == "add":
            if not is_admin:
                await callback.answer(
                    "Додавати профілі можуть лише адміністратори.",
                    show_alert=True,
                )
                return
            text = (
                "<b>➕ ДОДАТИ ПРОФІЛЬ</b>\n\n"
                "Надішліть у чат посилання на профіль або його @нік.\n\n"
                "Наприклад: <code>@username</code>\n"
                "Поточні відео не надійдуть повторно; сповіщення будуть "
                "лише про нові публікації."
            )
            await state.set_state(ProfileAddStates.waiting_for_profile)
        elif section == "save":
            if not is_admin:
                await callback.answer(
                    "Завантаження відео доступне лише адміністраторам.",
                    show_alert=True,
                )
                return
            text = (
                "<b>⬇️ ЗБЕРЕГТИ ВІДЕО</b>\n\n"
                "Надішліть у чат пряме посилання на публічне відео TikTok.\n"
                "Бот завантажить ролик, застосує фільтри та надішле файл."
            )
            await state.set_state(SaveVideoStates.waiting_for_url)
        elif section == "chatid":
            text = (
                "<b>🆔 ДАНІ ЧАТУ</b>\n\n"
                f"Ваш Telegram ID: <code>{callback.from_user.id}</code>\n"
                f"ID цього чату: <code>{callback.message.chat.id}</code>"
            )
        elif section == "help":
            text = (
                "<b>ℹ️ ДОПОМОГА</b>\n\n"
                "Користуйтеся кнопками головного меню: профілі, додавання, "
                "видалення, довідка та ID чату.\n\n"
                "Щоб додати профіль, натисніть «Додати профіль» і надішліть "
                "його @нік або посилання. Щоб зберегти відео, натисніть кнопку "
                "під сповіщенням.\n\n"
                "Завантаження й обробка медіа доступні адміністраторам."
            )
        else:
            await callback.answer("Ця дія меню більше не підтримується.")
            return

        await callback.message.edit_text(
            text,
            reply_markup=(
                _profiles_keyboard(profiles)
                if section == "profiles" and is_admin
                else InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="❌ Скасувати",
                                callback_data="menu:cancel_add",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🏠 Головне меню",
                                callback_data="menu:home",
                            )
                        ],
                    ]
                )
                if section == "add" and is_admin
                else InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="❌ Скасувати",
                                callback_data="menu:cancel_add",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🏠 Головне меню",
                                callback_data="menu:home",
                            )
                        ],
                    ]
                )
                if section == "save" and is_admin
                else _menu_keyboard(section, is_admin)
            ),
            parse_mode="HTML",
        )
        await callback.answer()

    @router.message(ProfileAddStates.waiting_for_profile, F.text)
    async def add_profile_text_handler(message: Message, state: FSMContext) -> None:
        raw_value = (message.text or "").strip()
        if raw_value.startswith("/"):
            await message.answer("Надішліть @нік або посилання на TikTok-профіль.")
            return
        if await add_profile_from_input(message, raw_value):
            await state.clear()

    @router.message(SaveVideoStates.waiting_for_url, F.text)
    async def save_video_text_handler(message: Message, state: FSMContext) -> None:
        raw_text = message.text or ""
        video_url = _extract_tiktok_video_link(raw_text)
        if video_url is None:
            await message.answer("Надішліть пряме посилання на відео TikTok.")
            return
        await state.clear()
        await send_saved_video(message, video_url)

    @router.message(F.text.func(lambda text: _extract_tiktok_video_link(text) is not None))
    async def tiktok_video_link_handler(message: Message) -> None:
        video_url = _extract_tiktok_video_link(message.text or "")
        if video_url is None:
            return
        await send_saved_video(message, video_url)

    @router.callback_query(F.data.startswith("profile:remove:"))
    async def remove_profile_callback(callback: CallbackQuery) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer("Це меню вже недоступне.", show_alert=True)
            return
        if callback.from_user.id not in settings.admin_ids:
            await callback.answer(
                "Видаляти профілі можуть лише адміністратори.",
                show_alert=True,
            )
            return
        raw_id = (callback.data or "").removeprefix("profile:remove:")
        if not raw_id.isdigit():
            await callback.answer("Некоректний ID профілю.", show_alert=True)
            return
        profile_id = int(raw_id)
        if not await database.remove_profile(profile_id):
            await callback.answer("Профіль не знайдено.", show_alert=True)
            return

        profiles = await database.list_profiles()
        entries = [
            f"• <b>#{profile['id']}</b> "
            f"<a href=\"{escape(str(profile['url']), quote=True)}\">"
            f"{escape(_truncate_utf16(str(profile['display_name']), 80))}</a>"
            for profile in profiles[:12]
        ]
        text = "<b>📋 ВІДСТЕЖУВАНІ ПРОФІЛІ</b>\n\n"
        text += "\n".join(entries) if entries else "Поки що профілів немає."
        await callback.message.edit_text(
            text,
            reply_markup=_profiles_keyboard(profiles),
            parse_mode="HTML",
        )
        await callback.answer(f"Профіль #{profile_id} видалено.")

    @router.message(Command("chatid"))
    async def chat_id_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else "невідомий"
        await message.answer(
            f"Ваш user ID: {user_id}\n"
            f"ID цього чату: {message.chat.id}"
        )

    async def process_uploaded_photo(message: Message) -> None:
        if not await _require_admin(message, settings):
            return
        if not message.photo:
            await message.answer("Надішліть фото як зображення.")
            return

        photo = message.photo[-1]
        if photo.file_size is not None and photo.file_size > MAX_INPUT_BYTES:
            await message.answer("Фото завелике. Максимальний розмір — 20 МБ.")
            return

        image_data = io.BytesIO()
        try:
            await message.bot.download(photo, destination=image_data)
        except TelegramAPIError as exc:
            logger.warning("Could not download photo from Telegram: %s", exc)
            await message.answer("Не вдалося отримати фото з Telegram. Спробуйте ще раз.")
            return
        if image_data.tell() > MAX_INPUT_BYTES:
            await message.answer("Фото завелике. Максимальний розмір — 20 МБ.")
            return

        try:
            enhanced = await asyncio.to_thread(enhance_image, image_data.getvalue())
        except MediaProcessingError as exc:
            logger.warning("Photo enhancement failed for chat %s: %s", message.chat.id, exc)
            await message.answer("Не вдалося обробити фото. Спробуйте інше зображення.")
            return

        caption = "Готово: фото оброблено без AI та без збільшення роздільності."
        photo_file = BufferedInputFile(enhanced, filename="enhanced.jpg")
        if len(enhanced) <= TELEGRAM_PHOTO_LIMIT_BYTES:
            await message.answer_photo(photo_file, caption=caption)
        else:
            await message.answer_document(photo_file, caption=caption)

    @router.message(F.photo)
    async def photo_message_handler(message: Message) -> None:
        await process_uploaded_photo(message)

    @router.message(F.video | (F.document & F.document.mime_type.startswith("video/")))
    async def video_message_handler(message: Message) -> None:
        if not await _require_admin(message, settings):
            return

        media = message.video or message.document
        if media is None:
            return
        if media.file_size is not None and media.file_size > MAX_INPUT_BYTES:
            await message.answer("Відео завелике. Максимальний розмір — 20 МБ.")
            return

        await message.answer("Обробляю відео фільтрами; це може зайняти кілька хвилин…")
        with tempfile.TemporaryDirectory(prefix="tiktok-upload-") as temp_dir:
            source = Path(temp_dir) / "input"
            try:
                await message.bot.download(media, destination=source)
            except TelegramAPIError as exc:
                logger.warning("Could not download video from Telegram: %s", exc)
                await message.answer(
                    "Не вдалося отримати відео з Telegram. Спробуйте інший файл."
                )
                return
            if source.stat().st_size > MAX_INPUT_BYTES:
                await message.answer("Відео завелике. Максимальний розмір — 20 МБ.")
                return
            output = Path(temp_dir) / "enhanced.mp4"
            try:
                await asyncio.to_thread(enhance_video, source, output)
            except MediaProcessingError as exc:
                logger.warning("Uploaded video enhancement failed: %s", exc)
                await message.answer(
                    "Не вдалося обробити відео. Перевірте формат і спробуйте коротший ролик."
                )
                return
            if output.stat().st_size > MAX_OUTPUT_BYTES:
                await message.answer(
                    "Після обробки відео перевищило ліміт Telegram (45 МБ). "
                    "Надішліть коротший ролик."
                )
                return
            await message.answer_video(
                FSInputFile(output, filename="enhanced.mp4"),
                caption="Готово: зниження шуму та легке підвищення різкості (без AI/4K).",
            )

    async def send_saved_video(
        message: Message,
        video_url: str,
        requester_id: int | None = None,
    ) -> None:
        if requester_id is None:
            if not await _require_admin(message, settings):
                return
        elif requester_id not in settings.admin_ids:
            await message.answer("Завантаження доступне лише адміністраторам.")
            return

        await message.answer("Завантажую відео TikTok…")
        with tempfile.TemporaryDirectory(prefix="tiktok-save-") as temp_dir:
            try:
                source = await asyncio.to_thread(
                    download_tiktok_video,
                    video_url,
                    Path(temp_dir),
                    MAX_OUTPUT_BYTES,
                )
            except TikTokError as exc:
                logger.warning("TikTok video download failed: %s", exc)
                await message.answer(
                    "Не вдалося завантажити відео. Перевірте, що посилання веде "
                    "на публічне відео TikTok, і спробуйте пізніше."
                )
                return

            output = Path(temp_dir) / "enhanced.mp4"
            try:
                await asyncio.to_thread(enhance_video, source, output)
                if output.stat().st_size > MAX_OUTPUT_BYTES:
                    raise MediaProcessingError("Оброблений файл перевищив ліміт.")
            except MediaProcessingError as exc:
                logger.warning(
                    "Could not enhance downloaded TikTok video; sending original: %s",
                    exc,
                )
                await message.answer(
                    "Не вдалося застосувати фільтри — надсилаю оригінал без змін."
                )
                output = source

            file = FSInputFile(output, filename=output.name)
            caption = "Відео готове до збереження в Telegram."
            if output.suffix.lower() in {".mp4", ".m4v"}:
                await message.answer_video(file, caption=caption)
            else:
                await message.answer_document(file, caption=caption)

    @router.callback_query(F.data.startswith("save:"))
    async def save_video_callback(callback: CallbackQuery) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer("Це повідомлення вже недоступне.", show_alert=True)
            return
        if callback.from_user.id not in settings.admin_ids:
            await callback.answer(
                "Завантаження доступне лише адміністраторам.",
                show_alert=True,
            )
            return
        video_id = (callback.data or "").removeprefix("save:")
        if not re.fullmatch(r"\d+", video_id):
            await callback.answer("Некоректний ID відео.", show_alert=True)
            return
        video_match = re.search(
            r"/@([A-Za-z0-9._-]+)/video/" + re.escape(video_id) + r"/?$",
            callback.message.text or callback.message.caption or "",
        )
        video_handle = video_match.group(1) if video_match else "_"
        await callback.answer("Завантажую відео…")
        await send_saved_video(
            callback.message,
            f"https://www.tiktok.com/@{video_handle}/video/{video_id}",
            requester_id=callback.from_user.id,
        )

    @router.message(Command("profiles"))
    async def profiles_handler(message: Message) -> None:
        if not await _require_admin(message, settings):
            return
        profiles = await database.list_profiles()
        if not profiles:
            await message.answer(
                "Поки немає профілів. Додайте профіль командою "
                "/add_profile <URL профілю>."
            )
            return
        for chunk in _split_messages(
            [_format_profile(profile) for profile in profiles]
        ):
            await message.answer(chunk)

    @router.message(Command("add_profile"))
    async def add_profile_handler(
        message: Message, command: CommandObject
    ) -> None:
        raw_url = (command.args or "").strip()
        if not raw_url:
            await message.answer("Натисніть «➕ Додати профіль» у меню.")
            return
        await add_profile_from_input(message, raw_url)

    @router.message(Command("remove_profile"))
    async def remove_profile_handler(
        message: Message, command: CommandObject
    ) -> None:
        if not await _require_admin(message, settings):
            return
        raw_id = (command.args or "").strip()
        try:
            profile_id = int(raw_id)
        except ValueError:
            await message.answer(
                "Вкажіть числовий ID зі списку:\n/remove_profile 1"
            )
            return

        if await database.remove_profile(profile_id):
            await message.answer(
                f"Профіль #{profile_id} і його історію відео видалено."
            )
        else:
            await message.answer(f"Профіль #{profile_id} не знайдено.")


async def send_video_notification(
    bot: Bot,
    settings: Settings,
    video: Video,
    profile_name: str,
) -> None:
    caption = build_caption(video, profile_name)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬇️ Зберегти відео",
                    callback_data=f"save:{video.video_id}",
                )
            ]
        ]
    )

    async def send_to_admin(chat_id: int) -> None:
        if video.thumbnail:
            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=video.thumbnail,
                    caption=caption,
                    reply_markup=keyboard,
                )
                return
            except TelegramAPIError as exc:
                logger.warning(
                    "Could not send thumbnail for %s to admin %s; "
                    "sending text instead: %s",
                    video.video_id,
                    chat_id,
                    exc,
                )

        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=keyboard,
        )

    results = await asyncio.gather(
        *(send_to_admin(chat_id) for chat_id in sorted(settings.admin_ids)),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def monitor_profiles(
    bot: Bot,
    database: Database,
    settings: Settings,
) -> None:
    """Періодично перевіряє профілі; yt-dlp виконується в окремому потоці."""
    while True:
        try:
            profiles = await database.list_profiles()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not load tracked profiles from Supabase")
            await asyncio.sleep(settings.poll_interval_seconds)
            continue

        for profile in profiles:
            profile_id = int(profile["id"])
            profile_url = str(profile["url"])
            profile_name = str(profile["display_name"])

            try:
                channel_name, latest_videos = await asyncio.to_thread(
                    fetch_latest_videos,
                    profile_url,
                    settings.max_videos_per_profile,
                )
                fallback_name = channel_name or profile_name
                fallback_handle = profile_url.rsplit("/@", 1)[-1]
                known_ids = await database.get_known_video_ids(
                    profile_id,
                    [video.video_id for video in latest_videos],
                )

                # yt-dlp повертає новіші відео першими; надсилаємо старіші спочатку.
                for listing_video in reversed(latest_videos):
                    if listing_video.video_id in known_ids:
                        continue

                    try:
                        video = await asyncio.to_thread(
                            fetch_video_details,
                            listing_video.url,
                            fallback_handle,
                        )
                    except TikTokError as exc:
                        # Опис або прев'ю можуть бути недоступні окремо; посилання
                        # та базові метадані зі сторінки каналу все одно достатні.
                        logger.warning(
                            "Using flat metadata for video %s: %s",
                            listing_video.video_id,
                            exc,
                        )
                        video = listing_video

                    try:
                        await send_video_notification(
                            bot,
                            settings,
                            video,
                            video.channel or fallback_name,
                        )
                    except TelegramAPIError:
                        logger.exception(
                            "Telegram delivery failed for video %s",
                            video.video_id,
                        )
                        continue

                    await database.mark_video_notified(
                        profile_id,
                        video.storage_record(),
                    )
                    logger.info(
                        "Sent notification for video %s from profile %s",
                        video.video_id,
                        profile_id,
                    )

                # Невелика пауза між профілями знижує навантаження на TikTok.
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Monitoring failed for profile %s (%s)",
                    profile_id,
                    profile_url,
                )

        await asyncio.sleep(settings.poll_interval_seconds)


async def _monitor_until_cancelled(
    bot: Bot,
    database: Database,
    settings: Settings,
) -> None:
    while True:
        try:
            await monitor_profiles(bot, database, settings)
            logger.warning("Profile monitor stopped; restarting in 30 seconds")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Profile monitor stopped unexpectedly; restarting in 30 seconds")
        await asyncio.sleep(30)


async def _log_heartbeat() -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        logger.info("Heartbeat: Telegram bot process is running")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    database = Database(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
    retry_delay = 5
    while True:
        try:
            await database.initialize()
            break
        except SupabaseError as exc:
            if not exc.retryable:
                raise
            logger.warning(
                "Supabase is temporarily unavailable; retrying in %s seconds: %s",
                retry_delay,
                exc,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)

    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    router = Router()
    register_handlers(router, database, settings)
    dispatcher.include_router(router)

    monitor_task: asyncio.Task | None = None
    heartbeat_task: asyncio.Task | None = None

    async def start_monitor(bot: Bot) -> None:
        nonlocal monitor_task, heartbeat_task
        monitor_task = asyncio.create_task(
            _monitor_until_cancelled(bot, database, settings),
            name="tiktok-profile-monitor",
        )
        heartbeat_task = asyncio.create_task(
            _log_heartbeat(),
            name="bot-heartbeat",
        )
        logger.info(
            "Monitoring started; checking every %s seconds",
            settings.poll_interval_seconds,
        )

    async def stop_monitor(bot: Bot) -> None:
        del bot  # параметр передається aiogram як контекст події завершення
        if monitor_task is not None:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    dispatcher.startup.register(start_monitor)
    dispatcher.shutdown.register(stop_monitor)

    port = int(os.getenv("PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT має бути числом від 1 до 65535.")
    web_runner = web.AppRunner(_create_health_app())

    try:
        await web_runner.setup()
        await web.TCPSite(web_runner, "0.0.0.0", port).start()
        logger.info("Health endpoint listening on port %s", port)
        await bot.set_my_commands([])
        await dispatcher.start_polling(bot)
    finally:
        try:
            await web_runner.cleanup()
        finally:
            try:
                await database.close()
            finally:
                await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ValueError as exc:
        raise SystemExit(f"Помилка конфігурації: {exc}") from exc

"""Telegram-бот: адмін-команди та фоновий моніторинг профілів TikTok."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from html import escape
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    FSInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .config import Settings, load_settings
from .media_processing import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    MediaProcessingError,
    enhance_video,
    normalize_video,
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
HEARTBEAT_INTERVAL_SECONDS = 300
BUTTON_ADD_PROFILE = "➕ Додати профіль"
BUTTON_PROFILES = "📋 Мої профілі"
BUTTON_SAVE_VIDEO = "⬇️ Скачать видео по ссылке"
BUTTON_REMOVE_PROFILE = "🗑 Видалити профіль"
BUTTON_CHAT_ID = "🆔 Мій chat ID"
BUTTON_HELP = "ℹ️ Довідка"
BUTTON_MAIN_MENU = "🏠 Головне меню"
BUTTON_CANCEL = "❌ Скасувати"
BUTTON_CONFIRM_DELETE = "✅ Так, видалити"


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


def _menu_keyboard(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = []
    if is_admin:
        rows.extend(
            [
                [
                    KeyboardButton(text=BUTTON_ADD_PROFILE),
                    KeyboardButton(text=BUTTON_PROFILES),
                ],
                [
                    KeyboardButton(text=BUTTON_SAVE_VIDEO),
                    KeyboardButton(text=BUTTON_REMOVE_PROFILE),
                ],
            ]
        )
    rows.append(
        [
            KeyboardButton(text=BUTTON_CHAT_ID),
            KeyboardButton(text=BUTTON_HELP),
        ]
    )
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть дію в меню",
    )


def _workflow_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_CANCEL)],
            [KeyboardButton(text=BUTTON_MAIN_MENU)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Надішліть посилання або нік",
    )


def _profile_selection_keyboard(profiles: list[dict]) -> ReplyKeyboardMarkup:
    rows = [
        [
            KeyboardButton(
                text=(
                    f"#{profile['id']} "
                    f"{_truncate_utf16(str(profile['display_name']), 32)}"
                )
            )
        ]
        for profile in profiles[:30]
    ]
    rows.extend(
        [
            [KeyboardButton(text=BUTTON_CANCEL)],
            [KeyboardButton(text=BUTTON_MAIN_MENU)],
        ]
    )
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть профіль",
    )


def _delete_confirmation_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_CONFIRM_DELETE)],
            [KeyboardButton(text=BUTTON_CANCEL)],
            [KeyboardButton(text=BUTTON_MAIN_MENU)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _convert_video_for_telegram(source: Path, destination: Path) -> bool:
    try:
        enhance_video(source, destination)
        return True
    except MediaProcessingError as filter_error:
        logger.warning("Video filters failed; converting to MP4 without filters: %s", filter_error)
        try:
            normalize_video(source, destination)
        except MediaProcessingError as conversion_error:
            raise MediaProcessingError(
                f"Could not convert video to MP4: {conversion_error}"
            ) from conversion_error
        return False


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


class ProfileDeleteStates(StatesGroup):
    selecting_profile = State()
    confirming_delete = State()

def register_handlers(
    router: Router,
    database: Database,
    settings: Settings,
) -> None:
    async def show_main_menu(message: Message) -> None:
        is_admin = _admin_only(message, settings)
        await message.answer(
            _main_menu_text(is_admin),
            reply_markup=_menu_keyboard(is_admin),
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
            await message.answer(
                "Цей профіль уже є у списку.",
                reply_markup=_menu_keyboard(_admin_only(message, settings)),
            )
            return True

        await message.answer(
            f"✅ Профіль «{channel_name}» додано.\n"
            f"Поточні відео ({len(existing_videos)}) позначені як уже відомі. "
            "Сповіщення надходитимуть про нові публікації.",
            reply_markup=_menu_keyboard(_admin_only(message, settings)),
        )
        return True

    @router.message(CommandStart())
    async def start_handler(message: Message, state: FSMContext) -> None:
        await state.clear()
        await show_main_menu(message)

    @router.message(F.text.in_({BUTTON_CANCEL, BUTTON_MAIN_MENU}))
    async def cancel_or_open_main_menu(
        message: Message,
        state: FSMContext,
    ) -> None:
        await state.clear()
        if message.text == BUTTON_CANCEL:
            await message.answer(
                "Дію скасовано.",
                reply_markup=_menu_keyboard(_admin_only(message, settings)),
            )
        else:
            await show_main_menu(message)

    @router.message(
        F.text.in_(
            {
                BUTTON_ADD_PROFILE,
                BUTTON_PROFILES,
                BUTTON_SAVE_VIDEO,
                BUTTON_REMOVE_PROFILE,
                BUTTON_CHAT_ID,
                BUTTON_HELP,
            }
        )
    )
    async def persistent_menu_handler(
        message: Message,
        state: FSMContext,
    ) -> None:
        await state.clear()
        text = message.text
        is_admin = _admin_only(message, settings)
        if text in {
            BUTTON_ADD_PROFILE,
            BUTTON_PROFILES,
            BUTTON_SAVE_VIDEO,
            BUTTON_REMOVE_PROFILE,
        } and not await _require_admin(message, settings):
            return

        if text == BUTTON_ADD_PROFILE:
            await state.set_state(ProfileAddStates.waiting_for_profile)
            await message.answer(
                "Надішліть @нік або посилання на TikTok-профіль.",
                reply_markup=_workflow_keyboard(),
            )
        elif text == BUTTON_SAVE_VIDEO:
            await state.set_state(SaveVideoStates.waiting_for_url)
            await message.answer(
                "Надішліть пряме посилання на публічне відео TikTok.",
                reply_markup=_workflow_keyboard(),
            )
        elif text == BUTTON_PROFILES:
            profiles = await database.list_profiles()
            if profiles:
                entries = [
                    f"• #{profile['id']} · {escape(str(profile['display_name']))}\n"
                    f"{escape(str(profile['url']))}"
                    for profile in profiles[:30]
                ]
                text = "<b>📋 ВІДСТЕЖУВАНІ ПРОФІЛІ</b>\n\n" + "\n\n".join(entries)
                if len(profiles) > 30:
                    text += f"\n\nПоказано 30 із {len(profiles)} профілів."
            else:
                text = "Поки що профілів немає. Натисніть «➕ Додати профіль»."
            await message.answer(
                text,
                reply_markup=_menu_keyboard(is_admin),
                parse_mode="HTML",
            )
        elif text == BUTTON_REMOVE_PROFILE:
            profiles = await database.list_profiles()
            if not profiles:
                await message.answer(
                    "Немає профілів для видалення.",
                    reply_markup=_menu_keyboard(is_admin),
                )
                return
            await state.set_state(ProfileDeleteStates.selecting_profile)
            await message.answer(
                "Оберіть профіль для видалення:",
                reply_markup=_profile_selection_keyboard(profiles),
            )
        elif text == BUTTON_CHAT_ID:
            user_id = message.from_user.id if message.from_user else "невідомий"
            await message.answer(
                f"Ваш Telegram ID: {user_id}\nID цього чату: {message.chat.id}",
                reply_markup=_menu_keyboard(is_admin),
            )
        elif text == BUTTON_HELP:
            await message.answer(
                "ℹ️ <b>ДОПОМОГА</b>\n\n"
                "Користуйтеся постійною клавіатурою внизу екрана.\n"
                "Додавання профілю: кнопка «Додати профіль», потім @нік або URL.\n"
                "Збереження відео: кнопка «Скачать видео по ссылке» або "
                "просто надішліть посилання TikTok.\n"
                "Фільтри фото вимкнені; відео надсилається в MP4.",
                reply_markup=_menu_keyboard(is_admin),
                parse_mode="HTML",
            )

    @router.message(ProfileDeleteStates.selecting_profile, F.text)
    async def profile_delete_selection_handler(
        message: Message,
        state: FSMContext,
    ) -> None:
        match = re.match(r"^#(\d+)\b", message.text or "")
        if match is None:
            await message.answer(
                "Оберіть профіль кнопкою зі списку або натисніть «❌ Скасувати»."
            )
            return
        profiles = await database.list_profiles()
        profile_id = int(match.group(1))
        profile = next(
            (item for item in profiles if int(item["id"]) == profile_id),
            None,
        )
        if profile is None:
            await message.answer("Профіль більше не знайдено. Оновіть список кнопкою меню.")
            await state.clear()
            return
        await state.update_data(profile_id=profile_id)
        await state.set_state(ProfileDeleteStates.confirming_delete)
        await message.answer(
            f"Видалити профіль «{escape(str(profile['display_name']))}» "
            f"(#{profile_id}) разом з історією відео?",
            reply_markup=_delete_confirmation_keyboard(),
            parse_mode="HTML",
        )

    @router.message(ProfileDeleteStates.confirming_delete, F.text == BUTTON_CONFIRM_DELETE)
    async def confirm_profile_delete_handler(
        message: Message,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        profile_id = data.get("profile_id")
        if not isinstance(profile_id, int):
            await state.clear()
            await message.answer(
                "Не вдалося визначити профіль. Відкрийте список і спробуйте ще раз.",
                reply_markup=_menu_keyboard(_admin_only(message, settings)),
            )
            return
        if await database.remove_profile(profile_id):
            await message.answer(
                f"Профіль #{profile_id} та його історію видалено.",
                reply_markup=_menu_keyboard(_admin_only(message, settings)),
            )
        else:
            await message.answer(
                f"Профіль #{profile_id} уже видалено.",
                reply_markup=_menu_keyboard(_admin_only(message, settings)),
            )
        await state.clear()

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

    @router.message(F.video | (F.document & F.document.mime_type.startswith("video/")))
    async def video_message_handler(message: Message, state: FSMContext) -> None:
        if not await _require_admin(message, settings):
            return
        await state.clear()

        media = message.video or message.document
        if media is None:
            return
        if media.file_size is not None and media.file_size > MAX_INPUT_BYTES:
            await message.answer(
                "Відео завелике. Максимальний розмір — 20 МБ.",
                reply_markup=_menu_keyboard(True),
            )
            return

        await message.answer(
            "Перетворюю відео у сумісний MP4…",
            reply_markup=_menu_keyboard(True),
        )
        with tempfile.TemporaryDirectory(prefix="tiktok-upload-") as temp_dir:
            source = Path(temp_dir) / "input"
            try:
                await message.bot.download(media, destination=source)
            except TelegramAPIError as exc:
                logger.warning("Could not download video from Telegram: %s", exc)
                await message.answer(
                    "Не вдалося отримати відео з Telegram. Спробуйте інший файл.",
                    reply_markup=_menu_keyboard(True),
                )
                return
            if source.stat().st_size > MAX_INPUT_BYTES:
                await message.answer(
                    "Відео завелике. Максимальний розмір — 20 МБ.",
                    reply_markup=_menu_keyboard(True),
                )
                return
            output = Path(temp_dir) / "enhanced.mp4"
            try:
                filters_applied = await asyncio.to_thread(
                    _convert_video_for_telegram,
                    source,
                    output,
                )
            except MediaProcessingError as exc:
                logger.warning("Uploaded video conversion failed: %s", exc)
                await message.answer(
                    "Не вдалося перетворити відео у MP4. Перевірте файл і спробуйте "
                    "коротший ролик.",
                    reply_markup=_menu_keyboard(True),
                )
                return
            if output.stat().st_size > MAX_OUTPUT_BYTES:
                await message.answer(
                    "Після обробки відео перевищило ліміт Telegram (45 МБ). "
                    "Надішліть коротший ролик.",
                    reply_markup=_menu_keyboard(True),
                )
                return
            await message.answer_video(
                FSInputFile(output, filename="enhanced.mp4"),
                caption=(
                    "Готово: MP4 (H.264/AAC). "
                    + (
                        "Застосовано легкі фільтри; без AI/4K."
                        if filters_applied
                        else "Надіслано без фільтрів; MP4 для сумісності."
                    )
                ),
                reply_markup=_menu_keyboard(True),
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

        await message.answer(
            "Завантажую відео TikTok…",
            reply_markup=_menu_keyboard(True),
        )
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
                filters_applied = await asyncio.to_thread(
                    _convert_video_for_telegram,
                    source,
                    output,
                )
                if output.stat().st_size > MAX_OUTPUT_BYTES:
                    raise MediaProcessingError("Оброблений файл перевищив ліміт.")
            except MediaProcessingError as exc:
                logger.exception("Could not convert downloaded TikTok video to MP4: %s", exc)
                await message.answer(
                    "Не вдалося перетворити відео у сумісний MP4. Оригінал не "
                    "надсилаю, щоб уникнути проблем із форматом.",
                    reply_markup=_menu_keyboard(True),
                )
                return

            caption = (
                "Видео готово для сохранения: MP4 (H.264/AAC)."
                if filters_applied
                else "Видео преобразовано в совместимый MP4; фильтры не применялись."
            )
            await message.answer_video(
                FSInputFile(output, filename="tiktok-video.mp4"),
                caption=caption,
                reply_markup=_menu_keyboard(True),
            )

async def send_video_notification(
    bot: Bot,
    settings: Settings,
    video: Video,
    profile_name: str,
) -> None:
    caption = build_caption(video, profile_name)

    async def send_to_admin(chat_id: int) -> None:
        if video.thumbnail:
            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=video.thumbnail,
                    caption=caption,
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

        await bot.send_message(chat_id=chat_id, text=caption)

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

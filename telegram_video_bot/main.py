"""Telegram-бот: адмін-команди та фоновий моніторинг профілів TikTok."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .config import Settings, load_settings
from .storage import Database, SupabaseError
from .tiktok import (
    TikTokError,
    Video,
    fetch_latest_videos,
    fetch_video_details,
    normalize_profile_url,
)

logger = logging.getLogger("tiktok_monitor")
TELEGRAM_CAPTION_LIMIT = 1024
HEARTBEAT_INTERVAL_SECONDS = 300


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


def _collect_hashtags(video: Video) -> str:
    hashtags = re.findall(r"#[\w]+", video.description, flags=re.UNICODE)
    for raw_tag in video.tags:
        tag = raw_tag.strip().lstrip("#")
        tag = re.sub(r"[^\w]+", "_", tag, flags=re.UNICODE).strip("_")
        if tag:
            hashtags.append(f"#{tag}")

    unique: list[str] = []
    seen: set[str] = set()
    for tag in hashtags:
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(tag)
    return " ".join(unique[:20])


def build_caption(video: Video, profile_name: str) -> str:
    """Формує один caption із прев'ю; зберігає посилання в межах 1024 символів."""
    profile = _truncate_utf16(profile_name.strip() or "TikTok", 120)
    title = _truncate_utf16(video.title.strip() or "Без назви", 240)
    hashtags = _truncate_utf16(_collect_hashtags(video), 240)
    link_line = f"Посилання: {video.url}"
    hashtags_line = f"\n\nХештеги: {hashtags}" if hashtags else ""

    prefix = f"Нове відео\nПрофіль: {profile}\nНазва: {title}\n\nОпис: "
    suffix = f"{hashtags_line}\n\n{link_line}"
    description = video.description.strip() or "Опис відсутній."

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


def _main_menu_text(is_admin: bool) -> str:
    access = "🛡 Доступ адміністратора" if is_admin else "👋 Відкритий доступ"
    return (
        "<b>✨ RADAR MEDIA</b>\n"
        "<i>Моніторинг нових відео TikTok</i>\n\n"
        "Відстежуйте свіжі публікації відкритих профілів "
        "і отримуйте сповіщення просто в Telegram.\n\n"
        f"🟢 Сервіс активний\n{access}\n\n"
        "<i>Оберіть дію в меню нижче.</i>"
    )


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

    @router.message(CommandStart())
    async def start_handler(message: Message) -> None:
        await show_main_menu(message)

    @router.message(Command("menu"))
    async def menu_handler(message: Message) -> None:
        await show_main_menu(message)

    @router.callback_query(F.data.startswith("menu:"))
    async def menu_callback_handler(callback: CallbackQuery) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer("Це меню вже недоступне.", show_alert=True)
            return

        is_admin = callback.from_user.id in settings.admin_ids
        section = (callback.data or "").removeprefix("menu:")
        if section == "home":
            text = _main_menu_text(is_admin)
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
                        "Повний список: /profiles</i>"
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
                "Надішліть команду, замінивши <code>@handle</code> "
                "на ім’я потрібного TikTok-профілю:\n\n"
                "<code>/add_profile https://www.tiktok.com/@handle</code>\n\n"
                "<i>Поточні відео збережуться як початкова історія. "
                "Сповіщення надходитимуть про нові публікації.</i>"
            )
        elif section == "chatid":
            text = (
                "<b>🆔 ДАНІ ЧАТУ</b>\n\n"
                f"Ваш Telegram ID: <code>{callback.from_user.id}</code>\n"
                f"ID цього чату: <code>{callback.message.chat.id}</code>"
            )
        elif section == "help":
            text = (
                "<b>ℹ️ ДОПОМОГА</b>\n\n"
                "📋 <code>/profiles</code> — список профілів\n"
                "➕ <code>/add_profile URL</code> — додати TikTok-профіль\n"
                "➖ <code>/remove_profile ID</code> — видалити профіль\n"
                "🆔 <code>/chatid</code> — показати ID чату\n"
                "🏠 <code>/menu</code> — відкрити це меню\n\n"
                "Команди керування доступні адміністраторам."
            )
        else:
            await callback.answer("Ця дія меню більше не підтримується.")
            return

        await callback.message.edit_text(
            text,
            reply_markup=_menu_keyboard(section, is_admin),
            parse_mode="HTML",
        )
        await callback.answer()

    @router.message(Command("chatid"))
    async def chat_id_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else "невідомий"
        await message.answer(
            f"Ваш user ID: {user_id}\n"
            f"ID цього чату: {message.chat.id}"
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
        if not await _require_admin(message, settings):
            return
        raw_url = (command.args or "").strip()
        if not raw_url:
            await message.answer(
                "Вкажіть URL профілю:\n/add_profile https://www.tiktok.com/@handle"
            )
            return

        try:
            profile_url = normalize_profile_url(raw_url)
        except ValueError as exc:
            await message.answer(str(exc))
            return

        await message.answer("Перевіряю профіль і фіксую його поточні відео…")
        try:
            channel_name, existing_videos = await asyncio.to_thread(
                fetch_latest_videos,
                profile_url,
                settings.max_videos_per_profile,
            )
        except TikTokError as exc:
            logger.warning("Could not add TikTok profile %s: %s", profile_url, exc)
            await message.answer(
                "Не вдалося перевірити профіль TikTok. Переконайтеся, що "
                "посилання відкрите й правильне, та спробуйте пізніше."
            )
            return

        profile_id = await database.add_profile(
            profile_url,
            channel_name,
            [video.storage_record() for video in existing_videos],
        )
        if profile_id is None:
            await message.answer("Цей канал уже є у списку.")
            return

        await message.answer(
            f"Профіль «{channel_name}» додано (ID {profile_id}). "
            f"Поточні відео ({len(existing_videos)}) позначені як уже відомі; "
            "нові публікації надсилатимуться окремо."
        )

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

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Відкрити головне меню"),
                BotCommand(command="menu", description="Показати inline-меню"),
                BotCommand(command="chatid", description="Показати ID чату"),
                BotCommand(command="profiles", description="Список профілів"),
                BotCommand(command="add_profile", description="Додати TikTok-профіль"),
                BotCommand(command="remove_profile", description="Видалити профіль"),
            ]
        )
        await dispatcher.start_polling(bot)
    finally:
        await database.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ValueError as exc:
        raise SystemExit(f"Помилка конфігурації: {exc}") from exc

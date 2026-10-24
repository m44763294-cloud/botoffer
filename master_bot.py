"""Main bot: provisions customer bots and supervises their worker processes."""
from __future__ import annotations

import asyncio
from html import escape
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestManagedBot,
    ManagedBotUpdated,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from managed_bots import ManagedBotStore


ROOT = Path(__file__).resolve().parent
load_dotenv(override=False)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("master_bot")
router = Router(name="master")
store = ManagedBotStore()
provision_lock = asyncio.Lock()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_IDS = {ADMIN_ID} if ADMIN_ID > 0 else set()
for _value in os.getenv("ADMIN_IDS", "").split(","):
    try:
        if _value.strip() and int(_value.strip()) > 0:
            ADMIN_IDS.add(int(_value.strip()))
    except ValueError:
        logger.warning("Некорректное значение в ADMIN_IDS")
MANUAL_URL = os.getenv("MANUAL_URL", "").strip()
PAID_ACCESS_CONTACT = "@oowellll"
PAID_ACCESS_URL = "https://t.me/oowellll"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_menu() -> InlineKeyboardMarkup:
    manual_button = (
        InlineKeyboardButton(text="📖 Инструкция", url=MANUAL_URL)
        if MANUAL_URL
        else InlineKeyboardButton(text="📖 Инструкция", callback_data="manual")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Создать бесплатного бота", callback_data="create:free")],
        [InlineKeyboardButton(text="Создать платного бота", callback_data="create:paid")],
        [InlineKeyboardButton(text=f"Написать админу {PAID_ACCESS_CONTACT} для платного доступа", url=PAID_ACCESS_URL)],
        [manual_button],
    ])


class ManagedWorkers:
    """Runs each customer bot in its own process with isolated application data."""

    def __init__(self, managed_store: ManagedBotStore) -> None:
        self.store = managed_store
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self.processes: dict[int, asyncio.subprocess.Process] = {}
        self.stopping = False

    async def start_existing(self) -> None:
        for bot_user_id in self.store.active_bot_ids():
            await self.ensure(bot_user_id)

    async def ensure(self, bot_user_id: int) -> None:
        task = self.tasks.get(bot_user_id)
        if task is None or task.done():
            self.tasks[bot_user_id] = asyncio.create_task(
                self._supervise(bot_user_id), name=f"managed-bot-{bot_user_id}"
            )

    async def restart(self, bot_user_id: int) -> None:
        process = self.processes.get(bot_user_id)
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        await self.ensure(bot_user_id)

    async def _supervise(self, bot_user_id: int) -> None:
        delay = 3
        log_dir = ROOT / "logs" / "managed"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{bot_user_id}.log"
        worker_env = os.environ.copy()
        for secret_name in (
            "MASTER_BOT_TOKEN",
            "BOT_TOKEN",
            "PAID_BOT_TOKEN",
            "MASTER_BOT_ENCRYPTION_KEY",
        ):
            worker_env.pop(secret_name, None)

        while not self.stopping:
            bot_config = self.store.get_bot_config(bot_user_id)
            if bot_config is None:
                return
            process = None
            try:
                token = self.store.get_bot_token(bot_user_id)
                if token is None:
                    return
                worker_env["BOT_SKIP_DOTENV"] = "1"
                if bot_config["profile"] == "free":
                    worker_env.pop("XROCKET_API_TOKEN", None)
                with log_path.open("a", encoding="utf-8") as worker_log:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-u",
                        str(ROOT / "managed_worker.py"),
                        str(bot_user_id),
                        str(bot_config["profile"]),
                        cwd=ROOT,
                        env=worker_env,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=worker_log,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    self.processes[bot_user_id] = process
                    assert process.stdin is not None
                    process.stdin.write(token.encode("utf-8") + b"\n")
                    await process.stdin.drain()
                    process.stdin.close()
                    del token
                    logger.info("Запущен дочерний бот с ID %s (pid %s)", bot_user_id, process.pid)
                    return_code = await process.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Не удалось запустить дочерний бот с ID %s", bot_user_id)
                if process is not None and process.returncode is None:
                    process.terminate()
                    await process.wait()
                return_code = -1
            finally:
                self.processes.pop(bot_user_id, None)

            if self.stopping:
                return
            logger.warning(
                "Дочерний бот с ID %s завершился (код %s); повторный запуск через %s сек.",
                bot_user_id,
                return_code,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def shutdown(self) -> None:
        self.stopping = True
        processes = [process for process in self.processes.values() if process.returncode is None]
        for process in processes:
            process.terminate()
        if processes:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(process.wait() for process in processes)), timeout=10
                )
            except asyncio.TimeoutError:
                for process in processes:
                    if process.returncode is None:
                        process.kill()
                await asyncio.gather(*(process.wait() for process in processes))
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def provision_managed_bot(
    *,
    bot: Bot,
    workers: ManagedWorkers,
    owner_id: int,
    owner_username: str | None,
    bot_user,
) -> None:
    """Fetch and safely persist a managed bot token, then start its worker."""
    if bot_user.id == bot.id:
        logger.warning("Пропущено подключение главного бота как дочернего (%s)", bot_user.id)
        try:
            await bot.send_message(
                owner_id,
                "Это главный бот конструктора. Его нельзя подключить как дочерний — "
                "создайте отдельного бота через меню конструктора.",
            )
        except TelegramAPIError:
            pass
        return

    async with provision_lock:
        try:
            token = await bot.get_managed_bot_token(user_id=bot_user.id)
        except TelegramAPIError:
            logger.error("Telegram не выдал токен для подконтрольного бота %s", bot_user.id)
            try:
                await bot.send_message(owner_id, "Не получилось подключить бота. Напишите владельцу сервиса.")
            except TelegramAPIError:
                pass
            return

        profile, changed = store.register_bot(
            owner_id=owner_id,
            owner_username=owner_username,
            bot_user_id=bot_user.id,
            bot_username=bot_user.username,
            bot_name=bot_user.first_name,
            token=token,
        )
    if profile is None:
        logger.warning(
            "Для созданного бота %s не найдено действующего запроса на создание", bot_user.id
        )
        try:
            await bot.send_message(
                owner_id,
                "Бот создан в Telegram, но запрос на его запуск устарел. Начните создание ещё раз из меню.",
            )
        except TelegramAPIError:
            pass
        return

    if changed:
        await workers.restart(bot_user.id)
    else:
        await workers.ensure(bot_user.id)
    logger.info("Подключён бот %s (тип %s), владелец %s", bot_user.id, profile, owner_id)
    if not changed:
        return
    try:
        await bot.send_message(
            owner_id,
            f"✅ Бот @{escape(bot_user.username or bot_user.first_name)} подключён.\n\n"
            + ("Доступ в платном боте открывается после оплаты 1 TON." if profile == "paid"
               else "Бесплатный доступ работает через обязательную группу."),
        )
    except TelegramAPIError:
        logger.info("Не удалось отправить подтверждение владельцу %s", owner_id)


@router.message(CommandStart())
async def start(message: Message) -> None:
    text = (
        "🤖 <b>Конструктор NFT-офер ботов</b>\n\n"
        "Создайте своего бота: он будет работать как зеркало офер-бота, "
        "а журнал сделок продолжит приходить администраторам сервиса.\n\n"
        "Бесплатный бот открывает доступ участникам обязательной группы. "
        "Чтобы получить разрешение на создание платного бота, напишите администратору "
        f"<a href=\"{PAID_ACCESS_URL}\">{PAID_ACCESS_CONTACT}</a>.\n\n"
        "Выберите действие:"
    )
    if message.from_user and is_admin(message.from_user.id):
        text += (
            "\n\n<b>Команды владельца</b>\n"
            "<code>/paid_access ID</code> — разрешить пользователю создавать платные боты\n"
            "<code>/paid_noaccess ID</code> — убрать это разрешение\n"
            "<code>/bots</code> — список созданных ботов\n"
            "<code>/token_reset ID</code> — отозвать старый токен дочернего бота"
        )
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu())


@router.callback_query(F.data == "manual")
async def manual_placeholder(callback: CallbackQuery) -> None:
    await callback.answer("Ссылка на инструкцию пока не добавлена.", show_alert=True)


@router.callback_query(F.data.startswith("create:"))
async def create_bot_request(callback: CallbackQuery, bot: Bot, workers: ManagedWorkers) -> None:
    profile = (callback.data or "").partition(":")[2]
    if profile not in {"free", "paid"}:
        await callback.answer("Неизвестный тип бота.", show_alert=True)
        return
    if profile == "paid" and not is_admin(callback.from_user.id) and not store.can_create_paid(callback.from_user.id):
        await callback.answer()
        await bot.send_message(
            callback.from_user.id,
            f"Для создания платного бота сначала запросите доступ у администратора {PAID_ACCESS_CONTACT}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"Написать {PAID_ACCESS_CONTACT}", url=PAID_ACCESS_URL)
            ]]),
        )
        return
    me = await bot.get_me()
    if not getattr(me, "can_manage_bots", False):
        await callback.answer(
            "Владелец должен включить Bot Management Mode для главного бота в BotFather.",
            show_alert=True,
        )
        return

    store.set_intent(callback.from_user.id, profile)
    request_id = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
    request_button = KeyboardButton(
        text="Создать бота в Telegram",
        request_managed_bot=KeyboardButtonRequestManagedBot(
            request_id=request_id,
            suggested_name="NFT Offer Bot",
            suggested_username="NFTOfferBot",
        ),
    )
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[request_button]], resize_keyboard=True, one_time_keyboard=True
    )
    await callback.answer()
    await bot.send_message(
        callback.from_user.id,
        "Telegram откроет форму создания. Укажите имя и свободный username бота, затем подтвердите создание.",
        reply_markup=keyboard,
    )


@router.message(F.managed_bot_created)
async def managed_bot_created(message: Message, bot: Bot, workers: ManagedWorkers) -> None:
    if message.from_user is None or message.managed_bot_created is None:
        return
    await provision_managed_bot(
        bot=bot,
        workers=workers,
        owner_id=message.from_user.id,
        owner_username=message.from_user.username,
        bot_user=message.managed_bot_created.bot,
    )


@router.managed_bot()
async def managed_bot_updated(managed: ManagedBotUpdated, bot: Bot, workers: ManagedWorkers) -> None:
    await provision_managed_bot(
        bot=bot,
        workers=workers,
        owner_id=managed.user.id,
        owner_username=managed.user.username,
        bot_user=managed.bot_user,
    )


async def _manage_paid_creation(message: Message, *, grant: bool) -> None:
    if message.from_user is None or not is_admin(message.from_user.id):
        return
    arguments = (message.text or "").split()
    if len(arguments) != 2:
        command = "/paid_access ID" if grant else "/paid_noaccess ID"
        await message.answer(f"Укажите Telegram ID пользователя: <code>{command}</code>", parse_mode="HTML")
        return
    try:
        user_id = int(arguments[1])
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("ID должен быть положительным числом.")
        return
    if grant:
        store.grant_paid_creation(user_id, message.from_user.id)
        await message.answer(f"✅ Разрешено создавать платные боты: <code>{user_id}</code>", parse_mode="HTML")
        try:
            await message.bot.send_message(user_id, "✅ Вам разрешено создавать платные боты через главное меню.")
        except TelegramAPIError:
            pass
    else:
        store.revoke_paid_creation(user_id)
        await message.answer(f"Разрешение на создание платных ботов убрано: <code>{user_id}</code>", parse_mode="HTML")


@router.message(Command("paid_access"))
async def grant_paid_creation(message: Message) -> None:
    await _manage_paid_creation(message, grant=True)


@router.message(Command("paid_noaccess"))
async def revoke_paid_creation(message: Message) -> None:
    await _manage_paid_creation(message, grant=False)


@router.message(Command("bots"))
async def list_managed_bots(message: Message) -> None:
    if message.from_user is None or not is_admin(message.from_user.id):
        return
    bots = store.all_active_bots()
    if not bots:
        await message.answer("Подключённых пользовательских ботов пока нет.")
        return
    lines = ["<b>Подключённые боты</b>"]
    for item in bots:
        username = item["bot_username"]
        identity = f"@{escape(str(username))}" if username else f"ID <code>{item['bot_user_id']}</code>"
        profile = "платный" if item["profile"] == "paid" else "бесплатный"
        lines.append(f"• {identity} — {profile}, владелец <code>{item['owner_id']}</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("token_reset"))
async def reset_managed_bot_token(message: Message, bot: Bot, workers: ManagedWorkers) -> None:
    if message.from_user is None or not is_admin(message.from_user.id):
        return
    arguments = (message.text or "").split()
    if len(arguments) != 2:
        await message.answer("Укажите ID бота: <code>/token_reset BOT_ID</code>", parse_mode="HTML")
        return
    try:
        bot_user_id = int(arguments[1])
        if bot_user_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("ID бота должен быть положительным числом.")
        return
    if store.get_bot_config(bot_user_id) is None:
        await message.answer("Активный бот с таким ID не найден.")
        return
    try:
        async with provision_lock:
            new_token = await bot.replace_managed_bot_token(user_id=bot_user_id)
            saved = store.replace_bot_token(bot_user_id, new_token)
    except TelegramAPIError:
        logger.error("Не удалось отозвать токен дочернего бота %s", bot_user_id)
        await message.answer("Telegram не смог заменить токен. Бот не изменён.")
        return
    if not saved:
        await message.answer("Токен заменён в Telegram, но бот не найден в активном реестре.")
        return
    await workers.restart(bot_user_id)
    await message.answer(
        f"✅ Старый токен бота <code>{bot_user_id}</code> отозван; рабочий процесс перезапускается с новым.",
        parse_mode="HTML",
    )


@router.message(Command("mybots"))
async def my_bots(message: Message) -> None:
    if message.from_user is None:
        return
    bots = store.bots_for_owner(message.from_user.id)
    if not bots:
        await message.answer("У вас пока нет подключённых ботов.")
        return
    lines = ["<b>Ваши боты</b>"]
    for item in bots:
        username = item["bot_username"]
        link = f"https://t.me/{username}" if username else "ссылка недоступна"
        profile = "платный" if item["profile"] == "paid" else "бесплатный"
        lines.append(
            f"• <a href=\"{link}\">{escape(str(item['bot_name']))}</a> — {profile}; "
            f"ID <code>{item['bot_user_id']}</code>"
        )
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def main() -> None:
    token = os.getenv("MASTER_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан MASTER_BOT_TOKEN в .env")

    bot = Bot(token=token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    workers = ManagedWorkers(store)
    try:
        me = await bot.get_me()
        if not getattr(me, "can_manage_bots", False):
            logger.warning("Для главного бота выключен Bot Management Mode в BotFather")
        logger.info("Главный бот запущен (@%s)", me.username)
        await workers.start_existing()
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot,
            workers=workers,
            allowed_updates=["message", "callback_query", "managed_bot"],
        )
    except TelegramUnauthorizedError:
        logger.error("Telegram отклонил токен главного бота. Проверьте MASTER_BOT_TOKEN.")
    finally:
        await workers.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

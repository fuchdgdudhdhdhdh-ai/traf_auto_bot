import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from telethon.errors import PhoneCodeInvalidError, PhoneCodeExpiredError

import settings
import broadcast
import monitor
from storage import load_json, save_json, add_subscriber
from states import LoginStates, SetMessageStates, AddGroupStates, AddSubscriberStates
from userbot_manager import (
    LoginSession,
    active_logins,
    get_ready_client,
    session_path_for,
    reset_account,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

bot = Bot(token=settings.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# Рассылка запускается строго вручную и только одна за раз - пока прошлая
# не закончится, новую не запустить (см. broadcast_from_user).
_broadcast_running = False


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


# ---------------------------------------------------------------- главное меню
#
# Логин в аккаунт РАССЫЛКИ отдельной кнопки не имеет - он запрашивается
# сам, ровно в момент нажатия "Разослать с личного акк.", если аккаунт ещё
# не авторизован. Логин в аккаунт МОНИТОРИНГА оставлен явной кнопкой,
# потому что сам мониторинг запускается автоматически при старте бота
# (работает 24/7) и ждать нажатия отдельной кнопки "запустить" ему не нужно -
# нужно только один раз войти в аккаунт.

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Войти: аккаунт мониторинга", callback_data="login:monitor")],
            [InlineKeyboardButton(text="ℹ️ Статус", callback_data="status")],
            [InlineKeyboardButton(text="➕ Добавить группу для мониторинга", callback_data="addgroup")],
            [InlineKeyboardButton(text="📄 Скачать базу рассылки", callback_data="getfile")],
            [InlineKeyboardButton(text="✏️ Изменить сообщение рассылки", callback_data="setmessage")],
            [InlineKeyboardButton(text="➕ Добавить получателя вручную", callback_data="addsubscriber")],
            [InlineKeyboardButton(text="📤 Разослать (с личного акк., без кнопок)", callback_data="broadcast:user")],
            [InlineKeyboardButton(text="📤 Разослать (от бота, с кнопками)", callback_data="broadcast:bot")],
            [InlineKeyboardButton(text="🧹 Сбросить данные", callback_data="resetmenu")],
        ]
    )


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Этот бот приватный.")
        return
    await state.clear()
    await message.answer("Панель управления кампанией:", reply_markup=main_menu())


@router.callback_query(F.data == "menu")
async def back_to_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Панель управления кампанией:", reply_markup=main_menu())


# ---------------------------------------------------------------- статус

@router.callback_query(F.data == "status")
async def status_cb(call: CallbackQuery):
    groups = load_json(settings.GROUPS_FILE, [])
    subs = load_json(settings.SUBSCRIBERS_FILE, [])
    msg = load_json(settings.BROADCAST_MESSAGE_FILE, None)

    monitor_client = await get_ready_client("monitor")
    monitor_authorized = monitor_client is not None
    if monitor_client:
        await monitor_client.disconnect()

    broadcast_client = await get_ready_client("broadcast")
    broadcast_authorized = broadcast_client is not None
    if broadcast_client:
        await broadcast_client.disconnect()

    text = (
        "📊 Статус\n\n"
        f"Мониторинг (24/7): {'🟢 подключён и слушает' if monitor.is_running() and monitor_authorized else ('🟡 ждёт входа в аккаунт' if not monitor_authorized else '🟡 запускается/ждёт группы')}\n"
        f"Групп в списке: {len(groups)}\n"
        f"В базе рассылки: {len(subs)}\n"
        f"Сообщение рассылки: {'задано ✅' if msg and msg.get('text') else 'не задано'}\n"
        f"Аккаунт рассылки: {'авторизован ✅' if broadcast_authorized else 'не авторизован (спросится при рассылке)'}\n"
        f"Рассылка сейчас: {'идёт 📤' if _broadcast_running else 'не запущена'}"
    )
    await call.message.edit_text(text, reply_markup=main_menu())


# ---------------------------------------------------------------- ленивый/явный вход в аккаунт

async def begin_login(entry, state: FSMContext, account: str, pending_action: str | None):
    await state.update_data(account=account, pending_action=pending_action)
    await state.set_state(LoginStates.waiting_phone)
    label = "мониторинга" if account == "monitor" else "рассылки"
    text = (
        f"Вход в аккаунт «{label}».\n"
        f"Отправьте номер телефона в формате +79991234567."
    )
    if isinstance(entry, CallbackQuery):
        await entry.message.answer(text)
    else:
        await entry.answer(text)


@router.callback_query(F.data == "login:monitor")
async def login_monitor_cb(call: CallbackQuery, state: FSMContext):
    client = await get_ready_client("monitor")
    if client:
        await client.disconnect()
        await call.answer("Аккаунт мониторинга уже авторизован.", show_alert=True)
        return
    await call.answer()
    await begin_login(call, state, "monitor", pending_action=None)


@router.message(LoginStates.waiting_phone)
async def got_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    data = await state.get_data()
    account = data["account"]

    login = LoginSession(session_path_for(account))
    active_logins[message.from_user.id] = login

    try:
        await login.request_code(phone)
    except Exception as e:
        await message.answer(f"Не удалось запросить код: {e}")
        return

    await state.set_state(LoginStates.waiting_code)
    await message.answer(
        "Код отправлен в Telegram. Введите его кнопками ниже:",
        reply_markup=code_keypad(""),
    )


# ---------------------------------------------------------------- логин: код (кнопки)

def code_keypad(entered: str) -> InlineKeyboardMarkup:
    rows = []
    digits = "1234567890"
    row = []
    for i, d in enumerate(digits, 1):
        row.append(InlineKeyboardButton(text=d, callback_data=f"digit:{d}"))
        if i % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text="⌫ Стереть", callback_data="digit:back"),
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="digit:ok"),
        ]
    )
    mask = "•" * len(entered) if entered else "(пусто)"
    rows.append([InlineKeyboardButton(text=f"Код: {mask}", callback_data="noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(LoginStates.waiting_code, F.data.startswith("digit:"))
async def code_digit(call: CallbackQuery, state: FSMContext):
    login = active_logins.get(call.from_user.id)
    if not login:
        await call.answer("Сессия входа потеряна, начните заново.", show_alert=True)
        return

    action = call.data.split(":")[1]

    if action == "back":
        login.entered_code = login.entered_code[:-1]
    elif action == "ok":
        if not login.entered_code:
            await call.answer("Сначала введите код.")
            return
        try:
            result = await login.submit_code()
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            await call.answer("Неверный или устаревший код.", show_alert=True)
            login.entered_code = ""
            await call.message.edit_reply_markup(reply_markup=code_keypad(""))
            return

        if result == "ok":
            await finish_login(call.message, state, call.from_user.id)
            return
        else:  # need_2fa
            await state.set_state(LoginStates.waiting_password)
            await call.message.edit_text(
                "На аккаунте включена двухфакторная аутентификация.\n"
                "Отправьте пароль (2FA) сообщением. Рекомендую потом удалить это сообщение из чата."
            )
            return
    else:
        if len(login.entered_code) < settings.MAX_CODE_LENGTH:
            login.entered_code += action

    await call.message.edit_reply_markup(reply_markup=code_keypad(login.entered_code))


@router.message(LoginStates.waiting_password)
async def got_password(message: Message, state: FSMContext):
    login = active_logins.get(message.from_user.id)
    if not login:
        await message.answer("Сессия входа потеряна, начните заново с /start.")
        return
    try:
        await login.submit_password(message.text.strip())
    except Exception as e:
        await message.answer(f"Не удалось войти: {e}")
        return
    await finish_login(message, state, message.from_user.id)


async def finish_login(message: Message, state: FSMContext, admin_id: int):
    data = await state.get_data()
    pending_action = data.get("pending_action")
    account = data.get("account")

    login = active_logins.pop(admin_id, None)
    if login:
        await login.disconnect()
    await state.clear()

    await message.answer("✅ Вход выполнен, сессия сохранена.")

    if account == "monitor":
        # фоновый цикл мониторинга уже работает 24/7 и сам ждал этого
        # логина - принудительно "толкаем" его, чтобы не ждать паузы ретрая
        await monitor.restart_now()
        await message.answer(
            "Мониторинг подхватит вход в течение нескольких секунд и будет "
            "работать постоянно (24/7).",
            reply_markup=main_menu(),
        )
        return

    if pending_action == "broadcast_user":
        client = await get_ready_client("broadcast")
        if client:
            await do_broadcast_user(message, client)
            return

    await message.answer("Панель управления кампанией:", reply_markup=main_menu())


# ---------------------------------------------------------------- группы для мониторинга

@router.callback_query(F.data == "addgroup")
async def addgroup_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddGroupStates.waiting_link)
    await call.message.edit_text(
        "Отправьте ссылку(и) на группу(ы), можно несколько строк за раз "
        "(например https://t.me/mygroup)."
    )


@router.message(AddGroupStates.waiting_link)
async def addgroup_save(message: Message, state: FSMContext):
    links = [l.strip() for l in message.text.splitlines() if l.strip()]
    groups = load_json(settings.GROUPS_FILE, [])
    groups.extend(l for l in links if l not in groups)
    save_json(settings.GROUPS_FILE, groups)
    await state.clear()

    # чтобы новые группы не ждали случайного обрыва связи, а подхватились сразу
    await monitor.restart_now()

    await message.answer(
        f"Добавлено групп: {len(links)}. Всего в списке: {len(groups)}.\n"
        f"Мониторинг (24/7) подхватит их в течение нескольких секунд.",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "getfile")
async def getfile_cb(call: CallbackQuery):
    subs = load_json(settings.SUBSCRIBERS_FILE, [])
    if not subs:
        await call.answer("База рассылки пока пуста.", show_alert=True)
        return

    lines = []
    for s in subs:
        who = f"@{s['username']}" if s.get("username") else str(s.get("id"))
        lines.append(f"{s.get('added_at', '')}\t{who}\t{s.get('source', '')}")

    export_path = f"{settings.DATA_DIR}/subscribers_export.txt"
    with open(export_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    await call.message.answer_document(FSInputFile(export_path), caption=f"Всего в базе: {len(subs)}")


# ---------------------------------------------------------------- сообщение рассылки

@router.callback_query(F.data == "setmessage")
async def setmessage_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(SetMessageStates.waiting_message)
    await call.message.edit_text(
        "Отправьте сообщение ТАК, как оно должно выглядеть у получателей:\n"
        "жирный/курсив/спойлер и премиум-эмодзи сохранятся автоматически, "
        "если они есть в вашем сообщении."
    )


@router.message(SetMessageStates.waiting_message)
async def setmessage_save(message: Message, state: FSMContext):
    entities = [e.model_dump() for e in (message.entities or [])]
    save_json(settings.BROADCAST_MESSAGE_FILE, {"text": message.text or "", "entities": entities})
    await state.clear()
    await message.answer("Сообщение для рассылки сохранено ✅", reply_markup=main_menu())


# ---------------------------------------------------------------- получатели (вручную)

@router.callback_query(F.data == "addsubscriber")
async def addsubscriber_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddSubscriberStates.waiting_contact)
    await call.message.edit_text(
        "Отправьте @username или id получателя(ей) вручную, можно несколько строк сразу.\n"
        "Уже существующие в базе будут пропущены."
    )


@router.message(AddSubscriberStates.waiting_contact)
async def addsubscriber_save(message: Message, state: FSMContext):
    contacts = [c.strip() for c in message.text.splitlines() if c.strip()]
    added = 0
    for c in contacts:
        if c.lstrip("-").isdigit():
            ok = add_subscriber(settings.SUBSCRIBERS_FILE, user_id=int(c), source="manual")
        else:
            ok = add_subscriber(settings.SUBSCRIBERS_FILE, username=c, source="manual")
        if ok:
            added += 1

    total = len(load_json(settings.SUBSCRIBERS_FILE, []))
    await state.clear()
    await message.answer(
        f"Добавлено новых: {added} (пропущено дублей: {len(contacts) - added}).\n"
        f"Всего в базе рассылки: {total}.",
        reply_markup=main_menu(),
    )


# ---------------------------------------------------------------- рассылка
#
# Запускается только вручную (по нажатию кнопки) и идёт до конца списка;
# ошибки на отдельных получателях скипаются, а не останавливают рассылку
# (см. broadcast.py про повтор через /start в @SpamBot). Пока рассылка не
# закончилась, повторный запуск заблокирован (_broadcast_running).

@router.callback_query(F.data == "broadcast:user")
async def broadcast_from_user(call: CallbackQuery, state: FSMContext):
    """Рассылка с личного (второго) аккаунта - полный форматинг и премиум-эмодзи, БЕЗ кнопок."""
    if _broadcast_running:
        await call.answer("Рассылка уже идёт, дождитесь окончания.", show_alert=True)
        return

    msg = load_json(settings.BROADCAST_MESSAGE_FILE, None)
    subs = load_json(settings.SUBSCRIBERS_FILE, [])
    if not msg or not msg.get("text"):
        await call.answer("Сначала задайте сообщение рассылки.", show_alert=True)
        return
    if not subs:
        await call.answer("База рассылки пуста.", show_alert=True)
        return

    client = await get_ready_client("broadcast")
    if not client:
        # аккаунт для рассылки запрашивается именно сейчас - ровно тогда, когда нужен
        await call.answer()
        await begin_login(call, state, "broadcast", pending_action="broadcast_user")
        return

    await call.answer()
    await do_broadcast_user(call.message, client)


async def do_broadcast_user(message: Message, client):
    global _broadcast_running

    msg = load_json(settings.BROADCAST_MESSAGE_FILE, None)
    subs = load_json(settings.SUBSCRIBERS_FILE, [])
    if not msg or not msg.get("text") or not subs:
        await message.answer("Сообщение или база рассылки пусты.", reply_markup=main_menu())
        await client.disconnect()
        return

    if _broadcast_running:
        await message.answer("Рассылка уже идёт, дождитесь окончания.", reply_markup=main_menu())
        await client.disconnect()
        return

    _broadcast_running = True
    await message.answer(f"📤 Отправляю {len(subs)} получателям с личного аккаунта. Это займёт время...")

    try:
        result = await broadcast.send_to_list(client, msg["text"], msg["entities"], subs)
    finally:
        _broadcast_running = False
        await client.disconnect()

    report = (
        f"Готово. Успешно (включая восстановленные после спам-блока): {len(result['ok'])}, "
        f"ошибок в отчёте: {len(result['failed'])}."
    )
    if result["recovered"]:
        report += f"\n♻️ Восстановлено после /start в @SpamBot: {len(result['recovered'])}."
    if result["failed"]:
        report += "\nОшибки (получатель: причина), но они всё равно зачтены как обработанные:\n" + "\n".join(
            f"{k}: {v}" for k, v in result["failed"].items()
        )

    await message.answer(report, reply_markup=main_menu())


@router.callback_query(F.data == "broadcast:bot")
async def broadcast_from_bot(call: CallbackQuery):
    """
    Рассылка от лица самого бота - поддерживает настоящие inline-кнопки,
    но получатель должен был хотя бы раз написать этому боту (/start),
    иначе Telegram не даёт боту написать первым. Отдельного входа в
    аккаунт не требуется - это обычный Bot API.
    """
    msg = load_json(settings.BROADCAST_MESSAGE_FILE, None)
    subs = load_json(settings.SUBSCRIBERS_FILE, [])
    if not msg or not msg.get("text"):
        await call.answer("Сначала задайте сообщение рассылки.", show_alert=True)
        return
    if not subs:
        await call.answer("База рассылки пуста.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=settings.ACK_BUTTON_TEXT, callback_data="ack")]]
    )

    sent, failed, skipped = 0, 0, 0
    for sub in subs:
        chat_id = sub.get("id")
        if chat_id is None:
            skipped += 1  # у бота нет способа написать первым только по username
            continue
        try:
            await bot.send_message(
                chat_id, msg["text"], entities=msg["entities"] or None, reply_markup=kb
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await call.answer("Готово.")
    await call.message.answer(
        f"Отправлено от бота: {sent}, ошибок: {failed}, пропущено (нет id): {skipped}."
    )


@router.callback_query(F.data == "ack")
async def ack_cb(call: CallbackQuery):
    await call.answer("👍")


@router.callback_query(F.data == "noop")
async def noop_cb(call: CallbackQuery):
    await call.answer()


# ---------------------------------------------------------------- сброс данных (сессии, группы и т.д.)

RESET_LABELS = {
    "monitor_session": "сессию аккаунта мониторинга",
    "broadcast_session": "сессию аккаунта рассылки",
    "groups": "список групп мониторинга",
    "subscribers": "базу рассылки (подписчиков)",
    "message": "сообщение рассылки",
}


def reset_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Сессия аккаунта мониторинга", callback_data="reset:monitor_session")],
            [InlineKeyboardButton(text="🔐 Сессия аккаунта рассылки", callback_data="reset:broadcast_session")],
            [InlineKeyboardButton(text="🔗 Список групп", callback_data="reset:groups")],
            [InlineKeyboardButton(text="📇 База рассылки (подписчики)", callback_data="reset:subscribers")],
            [InlineKeyboardButton(text="✏️ Сообщение рассылки", callback_data="reset:message")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )


@router.callback_query(F.data == "resetmenu")
async def resetmenu_cb(call: CallbackQuery):
    await call.message.edit_text("Что сбросить? Действие необратимо.", reply_markup=reset_menu())


@router.callback_query(F.data.startswith("reset:confirm:"))
async def reset_confirm_cb(call: CallbackQuery):
    target = call.data.split(":", 2)[2]
    text = await do_reset(target)
    await call.answer()
    await call.message.edit_text(text, reply_markup=main_menu())


@router.callback_query(F.data.startswith("reset:"))
async def reset_ask_cb(call: CallbackQuery):
    target = call.data.split(":", 1)[1]
    label = RESET_LABELS.get(target)
    if not label:
        await call.answer()
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, сбросить", callback_data=f"reset:confirm:{target}")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="resetmenu")],
        ]
    )
    await call.message.edit_text(f"Точно сбросить {label}? Это необратимо.", reply_markup=kb)


async def do_reset(target: str) -> str:
    if target == "monitor_session":
        await reset_account("monitor")
        await monitor.restart_now()
        return "🔐 Сессия аккаунта мониторинга сброшена. Потребуется повторный вход."
    if target == "broadcast_session":
        await reset_account("broadcast")
        return "🔐 Сессия аккаунта рассылки сброшена. Потребуется повторный вход при следующей рассылке."
    if target == "groups":
        save_json(settings.GROUPS_FILE, [])
        await monitor.restart_now()
        return "🔗 Список групп очищен."
    if target == "subscribers":
        save_json(settings.SUBSCRIBERS_FILE, [])
        return "📇 База рассылки очищена."
    if target == "message":
        save_json(settings.BROADCAST_MESSAGE_FILE, {"text": "", "entities": []})
        return "✏️ Сообщение рассылки сброшено."
    return "Неизвестная команда сброса."


# ---------------------------------------------------------------- keep-alive HTTP сервер
#
# Render "усыпляет" web-сервисы на бесплатном плане после ~15 минут без
# входящих HTTP-запросов. Этот лёгкий сервер отвечает на GET /health,
# а Render Cron Job (см. render.yaml) дёргает этот адрес каждые 10 минут,
# не давая сервису заснуть. На платных планах (Starter+) сервис и так не
# спит - но пинг всё равно безвреден.

async def _health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.HEALTHCHECK_PORT)
    await site.start()
    log.info("Keep-alive HTTP сервер поднят на порту %s", settings.HEALTHCHECK_PORT)


async def main():
    await start_health_server()

    # Мониторинг стартует автоматически и работает 24/7: сам ждёт логина
    # аккаунта и наличия групп, сам переподключается при обрывах связи.
    monitor.start_monitoring()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

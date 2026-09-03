"""
Управление Telethon-клиентами (юзер-аккаунтами).

Один и тот же класс используется и для "аккаунта мониторинга",
и для "аккаунта рассылки" - просто с разными файлами сессий.

⚠️ Про конкурентный доступ к файлу сессии
------------------------------------------
Telethon хранит сессию в SQLite-файле. Аккаунт МОНИТОРИНГА держит своё
подключение открытым 24/7 (см. monitor.py, run_until_disconnected()).
Если в этот момент где-то ещё открыть ВТОРОЙ TelegramClient на тот же файл
сессии (например, просто чтобы проверить статус или доступность группы),
Telethon падает с `sqlite3.OperationalError: database is locked` - два
подключения к одному sqlite-файлу сессии одновременно не поддерживаются.

Поэтому для account="monitor" get_ready_client() НЕ открывает новое
подключение, если фоновый мониторинг уже подключён - вместо этого отдаёт
его же живой клиент (см. monitor.get_active_client()). Такой клиент
считается "одолженным": получивший его код НЕ должен сам вызывать
client.disconnect() - иначе оборвёт работающий 24/7 мониторинг. Вместо
client.disconnect() используйте release_client(account, client).

Все места, которые всё же создают новый TelegramClient на файл сессии
(первый вход, сброс сессии), сериализованы через per-account
asyncio.Lock, чтобы даже пара быстрых нажатий подряд не открыла два
клиента на один файл одновременно.
"""
import asyncio
import glob
import os

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
)

import settings

_locks: dict[str, asyncio.Lock] = {}


def _lock_for(account: str) -> asyncio.Lock:
    return _locks.setdefault(account, asyncio.Lock())


def _active_monitor_client():
    """Локальный импорт - чтобы не словить циклический импорт monitor<->userbot_manager."""
    import monitor
    return monitor.get_active_client()


class LoginSession:
    """Хранит промежуточное состояние логина одного клиента (per admin, per account)."""

    def __init__(self, session_path: str):
        self.client = TelegramClient(session_path, settings.API_ID, settings.API_HASH)
        self.phone: str | None = None
        self.phone_code_hash: str | None = None
        self.entered_code: str = ""  # набирается кнопками-цифрами

    async def connect(self):
        if not self.client.is_connected():
            await self.client.connect()

    async def already_authorized(self) -> bool:
        await self.connect()
        return await self.client.is_user_authorized()

    async def request_code(self, phone: str):
        await self.connect()
        self.phone = phone
        sent = await self.client.send_code_request(phone)
        self.phone_code_hash = sent.phone_code_hash
        self.entered_code = ""

    async def submit_code(self) -> str:
        """
        Возвращает:
          "ok"        - вход выполнен
          "need_2fa"  - нужен пароль двухфакторки
        Кидает исключение при неверном/просроченном коде.
        """
        try:
            await self.client.sign_in(
                phone=self.phone,
                code=self.entered_code,
                phone_code_hash=self.phone_code_hash,
            )
            return "ok"
        except SessionPasswordNeededError:
            return "need_2fa"
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            raise

    async def submit_password(self, password: str):
        await self.client.sign_in(password=password)

    async def disconnect(self):
        if self.client.is_connected():
            await self.client.disconnect()


# Активные процессы логина, по admin_id -> LoginSession
active_logins: dict[int, LoginSession] = {}


def session_path_for(account: str) -> str:
    return settings.MONITOR_SESSION if account == "monitor" else settings.BROADCAST_SESSION


async def get_ready_client(account: str) -> TelegramClient | None:
    """
    Возвращает уже авторизованный клиент для мониторинга/рассылки, либо None.

    Для account="monitor": если фоновый мониторинг сейчас подключён, отдаёт
    ЕГО живой клиент вместо открытия второй sqlite-сессии (см. предупреждение
    в шапке файла). В этом случае вызывающий код обязан освобождать клиент
    через release_client(), а не client.disconnect() напрямую.
    """
    if account == "monitor":
        active = _active_monitor_client()
        if active is not None and active.is_connected():
            return active

    async with _lock_for(account):
        # повторная проверка внутри лока - вдруг мониторинг успел
        # подключиться, пока мы ждали
        if account == "monitor":
            active = _active_monitor_client()
            if active is not None and active.is_connected():
                return active

        path = session_path_for(account)
        client = TelegramClient(path, settings.API_ID, settings.API_HASH)
        await client.connect()
        if await client.is_user_authorized():
            return client
        await client.disconnect()
        return None


def is_borrowed_monitor_client(client: TelegramClient) -> bool:
    """True, если client - это одолженный живой клиент фонового мониторинга."""
    return client is _active_monitor_client()


async def release_client(account: str, client: TelegramClient | None):
    """
    Корректно освобождает клиент, полученный через get_ready_client():
    отключает его, ЕСЛИ это не одолженный живой клиент мониторинга (тот
    отключать нельзя - иначе оборвётся 24/7 мониторинг, monitor.py сам
    управляет его подключением/отключением).

    Используйте это вместо client.disconnect() везде, где клиент мог
    прийти из get_ready_client("monitor").
    """
    if client is None:
        return
    if account == "monitor" and is_borrowed_monitor_client(client):
        return
    if client.is_connected():
        await client.disconnect()


def remove_session_files(session_path: str) -> bool:
    """Удаляет файл(ы) сессии Telethon (.session, .session-journal и т.п.)."""
    removed_any = False
    for f in glob.glob(f"{session_path}.session*"):
        try:
            os.remove(f)
            removed_any = True
        except OSError:
            pass
    return removed_any


async def reset_account(account: str) -> bool:
    """
    Полностью сбрасывает вход для аккаунта monitor/broadcast: если он был
    авторизован - разлогинивает его на стороне Telegram (client.log_out),
    затем на всякий случай подчищает оставшиеся файлы сессии на диске.
    После этого для аккаунта снова потребуется полный вход (номер/код/2FA).

    Для account="monitor": если фоновый мониторинг сейчас подключён на этом
    аккаунте, log_out() выполняется прямо на ЕГО живом клиенте - вместо
    того чтобы параллельно открывать второй TelegramClient на тот же файл
    сессии (иначе тоже словили бы "database is locked"). log_out() сам
    отключает клиент, после чего цикл мониторинга в monitor.py увидит
    разрыв соединения и корректно перейдёт в режим ожидания нового входа.
    """
    async with _lock_for(account):
        active = _active_monitor_client() if account == "monitor" else None
        path = session_path_for(account)

        if active is not None and active.is_connected():
            client = active
            own_connection = False
        else:
            client = TelegramClient(path, settings.API_ID, settings.API_HASH)
            own_connection = True

        try:
            if own_connection:
                await client.connect()
            if await client.is_user_authorized():
                await client.log_out()  # сам разрывает соединение и чистит сессию
            elif own_connection and client.is_connected():
                await client.disconnect()
        except Exception:
            pass

    remove_session_files(path)
    return True

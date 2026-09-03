"""
Управление Telethon-клиентами (юзер-аккаунтами).

Один и тот же класс используется и для "аккаунта мониторинга",
и для "аккаунта рассылки" - просто с разными файлами сессий.
"""
import glob
import os

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
)

import settings


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
    """Возвращает уже авторизованный клиент для мониторинга/рассылки, либо None."""
    path = session_path_for(account)
    client = TelegramClient(path, settings.API_ID, settings.API_HASH)
    await client.connect()
    if await client.is_user_authorized():
        return client
    await client.disconnect()
    return None


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
    """
    path = session_path_for(account)
    client = TelegramClient(path, settings.API_ID, settings.API_HASH)
    try:
        await client.connect()
        if await client.is_user_authorized():
            await client.log_out()  # сам разрывает соединение и чистит сессию
        else:
            await client.disconnect()
    except Exception:
        pass
    remove_session_files(path)
    return True

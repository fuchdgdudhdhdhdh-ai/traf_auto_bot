"""
Разбор того, что администратор вводит при добавлении группы для мониторинга.

Поддерживаемые форматы (по одному в строке):
  - https://t.me/username                публичная ссылка на группу/канал
  - https://t.me/username/123            ссылка на КОНКРЕТНОЕ СООБЩЕНИЕ -
                                          самый надёжный вариант: однозначно
                                          указывает и чат, и точку отсчёта
  - https://t.me/c/1234567890/123        то же самое для приватной группы
                                          без публичного username (Telegram
                                          сам даёт такую ссылку через
                                          "Копировать ссылку" на сообщении)
  - https://t.me/+AbCdEfGh / joinchat/... ссылка-приглашение
  - @username                             просто юзернейм
  - -1001234567890 или 1234567890        числовой id группы/канала

Раз мониторинг слушает только НОВЫЕ сообщения (см. monitor.py), у любого
нового сообщения id всегда больше, чем у уже существующих на момент
добавления группы - поэтому start_message_id ничего не фильтрует, это
просто метка "с какого сообщения вы указали группу", для наглядности в
статусе/логах и (главное) как самый надёжный способ сослаться на приватный
чат без приглашения под рукой.
"""
import re
from typing import Optional

_MSG_LINK_PRIVATE_RE = re.compile(r"^https?://t\.me/c/(\d+)/(\d+)/?(?:\?.*)?$")
_MSG_LINK_PUBLIC_RE = re.compile(r"^https?://t\.me/([A-Za-z0-9_]{4,})/(\d+)/?(?:\?.*)?$")
_INVITE_LINK_RE = re.compile(r"^https?://t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)/?$")
_USERNAME_LINK_RE = re.compile(r"^https?://t\.me/([A-Za-z0-9_]{4,})/?(?:\?.*)?$")


def parse_group_input(raw: str) -> dict:
    """
    Возвращает {"input": <как ввели>, "chat_ref": <что передавать Telethon>,
    "start_message_id": <int|None>}.
    """
    text = raw.strip()

    m = _MSG_LINK_PRIVATE_RE.match(text)
    if m:
        internal_id, msg_id = m.groups()
        chat_ref = int(f"-100{internal_id}")
        return {"input": text, "chat_ref": chat_ref, "start_message_id": int(msg_id)}

    m = _MSG_LINK_PUBLIC_RE.match(text)
    if m:
        username, msg_id = m.groups()
        return {"input": text, "chat_ref": f"@{username}", "start_message_id": int(msg_id)}

    m = _INVITE_LINK_RE.match(text)
    if m:
        return {"input": text, "chat_ref": text, "start_message_id": None}

    m = _USERNAME_LINK_RE.match(text)
    if m:
        username = m.group(1)
        return {"input": text, "chat_ref": f"@{username}", "start_message_id": None}

    if text.startswith("@"):
        return {"input": text, "chat_ref": text, "start_message_id": None}

    cleaned = text.replace(" ", "")
    if cleaned.lstrip("-").isdigit():
        return {"input": text, "chat_ref": int(cleaned), "start_message_id": None}

    # неизвестный формат - сохраняем как есть, пусть Telethon попробует сам
    return {"input": text, "chat_ref": text, "start_message_id": None}


def same_chat(a: dict, b: dict) -> bool:
    """Дедупликация: одна и та же группа, если совпал chat_ref."""
    return a.get("chat_ref") == b.get("chat_ref")


def describe(group: dict) -> str:
    ref = group.get("chat_ref")
    start = group.get("start_message_id")
    label = str(ref)
    if start:
        label += f" (с сообщения #{start})"
    return label


def load_groups(path: str) -> list[dict]:
    """
    Читает groups.json и на лету мигрирует старый формат (список простых
    строк-ссылок из прошлой версии бота) в новый формат словарей.
    """
    from storage import load_json, save_json

    raw = load_json(path, [])
    normalized = []
    changed = False
    for g in raw:
        if isinstance(g, str):
            normalized.append(parse_group_input(g))
            changed = True
        elif isinstance(g, dict) and "chat_ref" in g:
            normalized.append(g)
        else:
            changed = True  # мусорная запись - выкидываем при миграции
    if changed:
        save_json(path, normalized)
    return normalized


async def resolve_entity(client, chat_ref):
    """
    Получает Telethon-entity по chat_ref. Для числового id, если клиент его
    ещё не "видел" (нет access_hash в кэше - типичная ситуация с чужим
    числовым id без предварительного контакта), пробует найти его среди
    диалогов аккаунта - для мониторинга группа и так должна быть в списке
    диалогов (аккаунт обязан состоять в ней).
    """
    try:
        return await client.get_entity(chat_ref)
    except Exception:
        if isinstance(chat_ref, int):
            async for dialog in client.iter_dialogs():
                if dialog.id == chat_ref:
                    return dialog.entity
        raise

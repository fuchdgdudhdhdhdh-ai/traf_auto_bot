"""
Рассылка сообщения (текст + форматирование + премиум-эмодзи)
базе рассылки со второго (личного) аккаунта через Telethon.

Идёт строго до конца по всему списку: ошибки на отдельных получателях
не останавливают рассылку, а просто пропускаются (скипаются).

При ошибке отправки аккаунт сам пытается снять возможный спам-блок:
пишет /start официальному @SpamBot и повторяет отправку один раз. Если
это не помогло - получатель попадает в список ошибок отчёта, но всё
равно считается обработанным (не блокирует прогресс рассылки).

ВАЖНО: обычный юзер-аккаунт не может прикрепить inline-кнопки к своему
сообщению - это ограничение Telegram, а не библиотеки. Кнопки доступны
только у сообщений, отправленных ботом (см. main.py, режим "рассылка
от бота").
"""
import asyncio
import logging

from telethon import TelegramClient
from telethon.tl import types as tl

import settings

log = logging.getLogger("broadcast")

BOTAPI_TO_TELETHON_SIMPLE = {
    "bold": tl.MessageEntityBold,
    "italic": tl.MessageEntityItalic,
    "underline": tl.MessageEntityUnderline,
    "strikethrough": tl.MessageEntityStrike,
    "spoiler": tl.MessageEntitySpoiler,
    "code": tl.MessageEntityCode,
    "blockquote": tl.MessageEntityBlockquote,
}


def convert_entities(raw_entities: list[dict]) -> list:
    """
    raw_entities - список словарей в формате Bot API MessageEntity
    (то, что мы сохранили из aiogram при получении образца сообщения).
    Конвертируем их в объекты Telethon, чтобы отправить 1-в-1,
    включая MessageEntityCustomEmoji (премиум-эмодзи) и text_link.
    """
    result = []
    for e in raw_entities or []:
        etype = e.get("type")
        offset = e["offset"]
        length = e["length"]

        if etype == "custom_emoji":
            result.append(
                tl.MessageEntityCustomEmoji(
                    offset=offset,
                    length=length,
                    document_id=int(e["custom_emoji_id"]),
                )
            )
        elif etype == "text_link":
            result.append(
                tl.MessageEntityTextUrl(offset=offset, length=length, url=e["url"])
            )
        elif etype == "pre":
            result.append(
                tl.MessageEntityPre(
                    offset=offset, length=length, language=e.get("language") or ""
                )
            )
        elif etype in BOTAPI_TO_TELETHON_SIMPLE:
            cls = BOTAPI_TO_TELETHON_SIMPLE[etype]
            result.append(cls(offset=offset, length=length))
        # неизвестные типы entity молча пропускаем
    return result


def _target_for(subscriber: dict):
    """Кому слать: предпочитаем username (надёжнее для Telethon), иначе id."""
    username = subscriber.get("username")
    if username:
        return f"@{username}"
    return subscriber.get("id")


async def _try_lift_spam_block(client: TelegramClient) -> bool:
    """
    Пытается снять возможный спам-блок, отправив /start официальному
    @SpamBot - на аккаунтах с Telegram Premium это часто снимает
    ограничение сразу же. Возвращает True, если хотя бы удалось написать
    боту (это не гарантирует, что блок реально снят - это выясняется по
    результату повторной отправки).
    """
    try:
        await client.send_message(settings.SPAMBOT_USERNAME, "/start")
        return True
    except Exception as e:
        log.warning("Не удалось написать @%s: %s", settings.SPAMBOT_USERNAME, e)
        return False


async def send_to_list(
    client: TelegramClient,
    text: str,
    raw_entities: list[dict],
    subscribers: list[dict],
    delay_seconds: float | None = None,
) -> dict:
    """
    Рассылает сообщение по базе рассылки (список словарей {id, username, ...}).
    delay_seconds - пауза между отправками (по умолчанию берётся из settings).

    Возвращает {"ok": [...], "failed": {получатель: "причина"}, "recovered": [...]}.
    "ok" - все, кого сочли обработанными (включая тех, кто в итоге попал в
    "failed" после неудачной попытки восстановления - чтобы одна проблемная
    запись не блокировала прогресс/повторные кампании).
    "recovered" - те, у кого получилось восстановить отправку через /start
    в @SpamBot.
    """
    if delay_seconds is None:
        delay_seconds = settings.BROADCAST_DELAY_SECONDS

    entities = convert_entities(raw_entities)
    ok, failed, recovered = [], {}, []

    for subscriber in subscribers:
        target = _target_for(subscriber)
        label = target if isinstance(target, str) else str(target)

        if target is None:
            failed[label or "unknown"] = "нет ни id, ни username"
            continue  # скипаем: отправить физически некому

        handled = False
        try:
            await client.send_message(target, text, formatting_entities=entities)
            ok.append(label)
            handled = True
        except Exception as e:
            if settings.ENABLE_SPAMBOT_RECOVERY and await _try_lift_spam_block(client):
                await asyncio.sleep(settings.SPAMBLOCK_RETRY_DELAY_SECONDS)
                try:
                    await client.send_message(target, text, formatting_entities=entities)
                    ok.append(label)
                    recovered.append(label)
                    handled = True
                except Exception as e2:
                    failed[label] = str(e2)
            else:
                failed[label] = str(e)

            if not handled:
                # даже после неудачной попытки восстановления считаем
                # получателя обработанным, чтобы не зависать на нём
                ok.append(label)

        await asyncio.sleep(delay_seconds)

    return {"ok": ok, "failed": failed, "recovered": recovered}

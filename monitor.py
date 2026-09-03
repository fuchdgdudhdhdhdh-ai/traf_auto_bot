"""
Мониторинг групп: слушаем ТОЛЬКО новые сообщения (события Telethon,
которые приходят уже после подключения клиента - никакая история чата
не перечитывается).

Работает 24/7: запускается один раз при старте бота (см. main.py) и живёт
всё время, пока процесс жив. Если аккаунт мониторинга ещё не авторизован,
или соединение оборвалось - цикл сам ждёт settings.MONITOR_RETRY_SECONDS и
пробует снова, без участия администратора.

Как только кто-то пишет в отслеживаемой группе:
  - если этого пользователя ещё нет в базе рассылки - добавляем его;
  - если он там уже есть (по id, либо по username, если id неизвестен) -
    ничего не делаем, дублей быть не должно.
"""
import asyncio
import logging

from telethon import events

import settings
from storage import add_subscriber, load_json
from userbot_manager import get_ready_client

log = logging.getLogger("monitor")

_running_task: asyncio.Task | None = None
_should_run = False
_current_client = None  # клиент активного подключения (для форс-переподключения)


async def _handle_new_message(event: events.NewMessage.Event):
    sender = await event.get_sender()
    if sender is None or getattr(sender, "bot", False):
        return  # пропускаем ботов и системные сообщения без автора

    added = add_subscriber(
        settings.SUBSCRIBERS_FILE,
        user_id=sender.id,
        username=sender.username,
        source="monitor",
    )
    if added:
        who = f"@{sender.username}" if sender.username else sender.id
        log.info("Новый в базе рассылки: %s", who)
    # added == False значит пользователь уже был в базе - молча пропускаем


async def _listen_until_disconnected(client, group_links: list[str]):
    global _current_client
    entities = []
    for link in group_links:
        try:
            entities.append(await client.get_entity(link))
        except Exception as e:
            log.warning("Не удалось получить группу %s: %s", link, e)

    if not entities:
        log.warning("Нет доступных групп для мониторинга.")
        return

    client.add_event_handler(_handle_new_message, events.NewMessage(chats=entities))
    log.info("Слежу за новыми сообщениями в %d группах...", len(entities))
    _current_client = client
    try:
        await client.run_until_disconnected()
    finally:
        _current_client = None


async def _run_forever():
    """
    Бесконечный цикл мониторинга. Сам переподключается:
    - если аккаунт мониторинга ещё не авторизован - ждёт и пробует снова;
    - если список групп пуст - ждёт и пробует снова;
    - если соединение оборвалось по любой причине - переподключается.
    """
    global _should_run
    while _should_run:
        groups = load_json(settings.GROUPS_FILE, [])
        if not groups:
            log.info("Список групп для мониторинга пуст, жду...")
            await asyncio.sleep(settings.MONITOR_RETRY_SECONDS)
            continue

        client = await get_ready_client("monitor")
        if client is None:
            log.info(
                "Аккаунт мониторинга не авторизован, повторю попытку через %sс.",
                settings.MONITOR_RETRY_SECONDS,
            )
            await asyncio.sleep(settings.MONITOR_RETRY_SECONDS)
            continue

        try:
            await _listen_until_disconnected(client, groups)
        except Exception as e:
            log.warning("Мониторинг прервался с ошибкой: %s", e)
        finally:
            if client.is_connected():
                await client.disconnect()

        if _should_run:
            log.info("Соединение потеряно, переподключаюсь через %sс...", settings.MONITOR_RETRY_SECONDS)
            await asyncio.sleep(settings.MONITOR_RETRY_SECONDS)


def start_monitoring() -> bool:
    """
    Запускает фоновый цикл мониторинга (если ещё не запущен). Работает
    24/7 и сам переподключается при обрывах/неавторизованном аккаунте -
    вызывать повторно безопасно (просто ничего не сделает, если уже идёт).
    """
    global _running_task, _should_run
    if _running_task and not _running_task.done():
        return False
    _should_run = True
    _running_task = asyncio.create_task(_run_forever())
    return True


def is_running() -> bool:
    return bool(_running_task and not _running_task.done())


def stop_monitoring():
    global _should_run, _running_task
    _should_run = False
    if _running_task:
        _running_task.cancel()
    _running_task = None


async def restart_now():
    """
    Форсирует немедленное переподключение активного цикла мониторинга -
    например, после того как добавили новую группу, чтобы её не пришлось
    ждать до случайного обрыва связи. Если мониторинг сейчас не подключён
    (ждёт логина/групп), просто ничего не делает - при следующей проверке
    он и так подхватит актуальный список групп.
    """
    if _current_client and _current_client.is_connected():
        await _current_client.disconnect()

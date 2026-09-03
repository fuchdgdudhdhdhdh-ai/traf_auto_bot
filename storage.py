import json
import os
import datetime
from typing import Any, Optional


# ---------------------------------------------------------------- базовые операции с файлами

def load_json(path: str, default: Any):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default


def save_json(path: str, data: Any):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_line(path: str, line: str):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def read_lines(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [l.rstrip("\n") for l in f if l.strip()]


# ---------------------------------------------------------------- база рассылки (подписчики)
#
# Каждая запись: {"id": int|None, "username": str|None, "source": "monitor"|"manual",
#                  "added_at": "2026-09-02T12:00:00"}
#
# Дедупликация: считаем пользователя уже добавленным, если совпал numeric id,
# либо (когда id неизвестен) совпал username без учёта регистра.

def load_subscribers(path: str) -> list[dict]:
    return load_json(path, [])


def save_subscribers(path: str, subs: list[dict]):
    save_json(path, subs)


def subscriber_exists(subs: list[dict], user_id: Optional[int] = None, username: Optional[str] = None) -> bool:
    uname = (username or "").lstrip("@").lower() or None
    for s in subs:
        if user_id is not None and s.get("id") == user_id:
            return True
        if uname and (s.get("username") or "").lower() == uname:
            return True
    return False


def add_subscriber(
    path: str,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    source: str = "monitor",
) -> bool:
    """
    Добавляет пользователя в базу рассылки, ЕСЛИ его там ещё нет.
    Возвращает True, если реально добавили; False, если пользователь
    уже был в базе (или не переданы ни id, ни username).
    """
    if user_id is None and not username:
        return False

    subs = load_subscribers(path)
    if subscriber_exists(subs, user_id, username):
        return False

    subs.append(
        {
            "id": user_id,
            "username": (username or "").lstrip("@") or None,
            "source": source,
            "added_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
    )
    save_subscribers(path, subs)
    return True

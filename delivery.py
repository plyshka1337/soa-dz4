"""Учебный автомат доставки. Время и ответы провайдеров задаёт вызывающий код."""

import json
import sqlite3
from dataclasses import dataclass
from uuid import uuid4


PLANS = {
    "push": ("push", "sms", "email"),
    "sms": ("sms", "push", "email"),
    "email": ("email", "push", "sms"),
}
TIMEOUTS = {"push": 2, "sms": 5, "email": 10}


@dataclass(frozen=True)
class Attempt:
    key: str
    notification_id: str
    channel: str
    text: str


class Delivery:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                producer TEXT NOT NULL,
                event_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                request TEXT NOT NULL,
                text TEXT NOT NULL,
                plan TEXT NOT NULL,
                state TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT -1,
                deadline REAL,
                UNIQUE (producer, event_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                key TEXT PRIMARY KEY,
                notification_id TEXT NOT NULL REFERENCES notifications(id),
                position INTEGER NOT NULL,
                channel TEXT NOT NULL,
                state TEXT NOT NULL,
                UNIQUE (notification_id, position)
            );
        """)

    def close(self):
        self.db.close()

    def create(self, event_id, user_id, text, *, producer="payments",
               kind="transactional", preferred="push", disabled=False,
               available=("push", "sms", "email")):
        """Принять событие или вернуть ID ранее принятого идентичного запроса."""
        if not all(isinstance(value, str) and value.strip()
                   for value in (event_id, user_id, text, producer)):
            raise ValueError("Событие, клиент, текст и отправитель обязательны")
        if kind not in ("transactional", "service", "marketing"):
            raise ValueError("Неизвестный тип уведомления")
        if preferred not in PLANS or any(c not in TIMEOUTS for c in available):
            raise ValueError("Неизвестный канал")
        if kind == "transactional" and disabled:
            raise ValueError("Критичные уведомления нельзя отключить")

        plan = [channel for channel in PLANS[preferred] if channel in available]
        if not plan and not disabled:
            raise ValueError("Нет проверенного доступного контакта")
        if kind != "transactional":
            plan = plan[:1]
        request = json.dumps([text, kind, preferred, disabled, plan])

        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT id, request FROM notifications "
                "WHERE producer = ? AND event_id = ? AND user_id = ?",
                (producer, event_id, user_id),
            ).fetchone()
            if existing:
                if existing["request"] != request:
                    raise ValueError("Событие уже принято с другим содержанием")
                return existing["id"]

            notification_id = str(uuid4())
            self.db.execute(
                "INSERT INTO notifications "
                "(id, producer, event_id, user_id, request, text, plan, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (notification_id, producer, event_id, user_id, request,
                 text, json.dumps(plan), "SUPPRESSED" if disabled else "PENDING"),
            )
            return notification_id

    def next_attempt(self, notification_id, now):
        """Подготовить отправку. None означает ожидание или завершение."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            item = self._notification(notification_id)
            if item["state"] not in ("PENDING", "WAITING"):
                return None
            if item["state"] == "WAITING":
                previous = self.db.execute(
                    "SELECT * FROM attempts "
                    "WHERE notification_id = ? AND position = ?",
                    (notification_id, item["position"]),
                ).fetchone()
                if now < item["deadline"]:
                    # После сбоя повторяется прежний ключ, а не создаётся попытка.
                    if previous["state"] == "SENDING":
                        return Attempt(previous["key"], notification_id,
                                       previous["channel"], item["text"])
                    return None
                self.db.execute(
                    "UPDATE attempts SET state = 'UNKNOWN' "
                    "WHERE key = ? AND state IN ('SENDING', 'ACCEPTED')",
                    (previous["key"],),
                )

            position = item["position"] + 1
            plan = json.loads(item["plan"])
            if position == len(plan):
                self.db.execute(
                    "UPDATE notifications SET state = 'NEEDS_ATTENTION' WHERE id = ?",
                    (notification_id,),
                )
                return None

            channel = plan[position]
            key = f"{notification_id}:{position}"
            # Резервирование фиксируется до внешнего вызова.
            self.db.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, 'SENDING')",
                (key, notification_id, position, channel),
            )
            self.db.execute(
                "UPDATE notifications SET state = 'WAITING', position = ?, "
                "deadline = ? WHERE id = ?",
                (position, now + TIMEOUTS[channel], notification_id),
            )
            return Attempt(key, notification_id, channel, item["text"])

    def record(self, key, result, now):
        """Записать доверенный результат попытки, включая позднее подтверждение."""
        if result not in ("accepted", "failed", "unknown", "delivered"):
            raise ValueError("Неизвестный результат")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            attempt = self.db.execute(
                "SELECT * FROM attempts WHERE key = ?", (key,),
            ).fetchone()
            if attempt is None:
                raise KeyError(key)
            item = self._notification(attempt["notification_id"])

            # Доставка терминальна: поздняя ошибка не отменяет успех.
            if attempt["state"] == "DELIVERED":
                return
            if result == "delivered":
                self.db.execute(
                    "UPDATE attempts SET state = 'DELIVERED' WHERE key = ?", (key,),
                )
                self.db.execute(
                    "UPDATE notifications SET state = 'DELIVERED' WHERE id = ?",
                    (item["id"],),
                )
            elif (attempt["state"] in ("SENDING", "ACCEPTED")
                  or (attempt["state"] == "UNKNOWN" and result == "failed")):
                self.db.execute(
                    "UPDATE attempts SET state = ? WHERE key = ?",
                    (result.upper(), key),
                )
                if (result == "failed" and item["state"] == "WAITING"
                        and item["position"] == attempt["position"]):
                    self.db.execute(
                        "UPDATE notifications SET deadline = ? WHERE id = ?",
                        (now, item["id"]),
                    )

    def get(self, notification_id):
        item = dict(self._notification(notification_id))
        item.pop("request")
        item["plan"] = json.loads(item["plan"])
        item["attempts"] = [dict(row) for row in self.db.execute(
            "SELECT key, channel, state FROM attempts "
            "WHERE notification_id = ? ORDER BY position", (notification_id,),
        )]
        return item

    def _notification(self, notification_id):
        item = self.db.execute(
            "SELECT * FROM notifications WHERE id = ?", (notification_id,),
        ).fetchone()
        if item is None:
            raise KeyError(notification_id)
        return item

"""Детерминированные примеры: ожидание моделируется числами, без sleep и сети."""

from delivery import Delivery


def show(title, delivery, notification_id):
    item = delivery.get(notification_id)
    history = ", ".join(f"{a['channel']}={a['state']}" for a in item["attempts"])
    print(f"{title}: {item['state']} [{history}]")


def main():
    delivery = Delivery()
    try:
        notification_id = delivery.create("transfer-1", "client-1", "Перевод выполнен")
        push = delivery.next_attempt(notification_id, now=0)
        delivery.record(push.key, "accepted", now=0)
        delivery.record(push.key, "delivered", now=1)
        show("1. Основной канал", delivery, notification_id)

        notification_id = delivery.create("transfer-2", "client-1", "Перевод выполнен")
        push = delivery.next_attempt(notification_id, now=0)
        delivery.record(push.key, "accepted", now=0)
        sms = delivery.next_attempt(notification_id, now=2)
        delivery.record(sms.key, "delivered", now=3)
        show("2. Нет push-подтверждения → SMS", delivery, notification_id)
        delivery.record(push.key, "delivered", now=4)
        show("3. Поздний push: возможен физический повтор", delivery, notification_id)

        notification_id = delivery.create("transfer-3", "client-1", "Перевод выполнен")
        for second in range(3):
            attempt = delivery.next_attempt(notification_id, now=second)
            delivery.record(attempt.key, "failed", now=second)
        delivery.next_attempt(notification_id, now=3)
        show("4. Все каналы отказали → разбор", delivery, notification_id)

        notification_id = delivery.create("offer-1", "client-1", "Предложение",
                                          kind="marketing", disabled=True)
        show("5. Маркетинг отключён", delivery, notification_id)
    finally:
        delivery.close()


if __name__ == "__main__":
    main()

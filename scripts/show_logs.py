"""
Вспомогательный просмотр журнала событий и диалогов из БД (НЕ часть приложения).

Показывает, что произошло с сообщениями клиентов и почему AI ответил именно так
(разделы 16–17 ТЗ). Читает DATABASE_URL из .env — та же БД, что у приложения.

Запуск (из корня проекта):
    python scripts/show_logs.py              # последние 30 событий и все диалоги
    python scripts/show_logs.py --limit 100  # больше событий
    python scripts/show_logs.py --errors     # только WARNING/ERROR/CRITICAL
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from database import SessionLocal  # noqa: E402
from models import Conversation, Customer, LogLevel, Message, SystemLog  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Журнал событий LeadPilot")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--errors", action="store_true", help="только WARNING и выше")
    args = parser.parse_args()

    with SessionLocal() as db:
        stmt = select(SystemLog).order_by(SystemLog.id.desc()).limit(args.limit)
        if args.errors:
            stmt = stmt.where(SystemLog.level != LogLevel.INFO)
        logs = list(reversed(db.scalars(stmt).all()))

        print(f"=== system_logs (последние {len(logs)}) ===")
        for row in logs:
            meta = row.payload or {}
            trace = " ".join(
                f"{key}={meta[key]}"
                for key in (
                    "message_id",
                    "decision",
                    "intent",
                    "escalation_reason",
                    "delivery_status",
                    "error",
                )
                if meta.get(key) is not None
            )
            print(
                f"{row.id:>4} {row.created_at:%H:%M:%S} {row.level.value:<8} "
                f"{row.event_type:<26} biz={row.business_id} {row.message}"
                + (f"\n       {trace}" if trace else "")
            )

        print("\n=== Диалоги ===")
        rows = db.execute(
            select(Conversation, Customer)
            .join(Customer, Customer.id == Conversation.customer_id)
            .order_by(Conversation.id)
        ).all()
        if not rows:
            print("(пока нет)")
        for conversation, customer in rows:
            print(
                f"#{conversation.id} biz={conversation.business_id} "
                f"{customer.name or '-'} (@{customer.username or '-'}) "
                f"статус={conversation.status.value} приоритет={conversation.priority.value} "
                f"причина={conversation.attention_reason or '-'}"
            )
            messages = db.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.id)
            ).all()
            for message in messages:
                state = (
                    f"обработка={message.processing_status.value}"
                    if message.processing_status
                    else f"доставка={message.delivery_status.value if message.delivery_status else '-'}"
                )
                text = message.text.replace("\n", " ")[:90]
                print(f"    [{message.sender_type.value:<8}] {state:<18} {text}")


if __name__ == "__main__":
    main()

"""
Демо-данные для кабинета (НЕ часть приложения): компания-барбершоп с услугами,
клиентами, диалогами, лидами и ответами менеджера — чтобы посмотреть кабинет
без Telegram-бота и реальных клиентов.

    python scripts/seed_demo.py

Создаёт владельца demo@example.com (пароль Demo-Pass-123) и менеджера
manager@example.com (тот же пароль). Повторный запуск ничего не дублирует.
Данные попадают в БД из DATABASE_URL (.env) — не запускайте на рабочей базе.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from ai.pipeline import REPLY_BOOKING_REQUEST, REPLY_NO_INFO  # noqa: E402
from database import SessionLocal  # noqa: E402
from models import (  # noqa: E402
    AiResponse,
    AiResponseStatus,
    AiTone,
    Business,
    BusinessMember,
    Channel,
    Conversation,
    ConversationStatus,
    Customer,
    DeliveryStatus,
    Lead,
    LeadPriority,
    LeadStatus,
    MemberRole,
    Message,
    ProcessingStatus,
    SenderType,
    Service,
    User,
    UserRole,
    UserStatus,
    utcnow,
)
from services import admin_service  # noqa: E402
from services.auth_service import hash_password  # noqa: E402

OWNER_EMAIL = "demo@example.com"
MANAGER_EMAIL = "manager@example.com"
PASSWORD = "Demo-Pass-123"  # noqa: S105 - пароль демо-аккаунта, только для локальной проверки

# Шаблоны — те же, что отправляет pipeline (ai/pipeline.py), чтобы демо не расходилось с кодом.
HOLD = REPLY_NO_INFO
BOOKING_HOLD = REPLY_BOOKING_REQUEST
PRICE_REPLY = (
    "Актуальные цены: Борода — 1 000 ₽, Стрижка — 1 500 ₽. Подскажите, что вас интересует?"
)


def main() -> None:
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == OWNER_EMAIL)):
            print(f"Демо-данные уже есть. Вход: {OWNER_EMAIL} / {PASSWORD}")
            return

        owner = User(
            email=OWNER_EMAIL,
            password_hash=hash_password(PASSWORD),
            role=UserRole.OWNER,
            status=UserStatus.ACTIVE,
        )
        manager = User(
            email=MANAGER_EMAIL,
            password_hash=hash_password(PASSWORD),
            role=UserRole.OWNER,
            status=UserStatus.ACTIVE,
        )
        db.add_all([owner, manager])
        db.flush()

        business = Business(
            owner_id=owner.id,
            name="Барбершоп «Бритва»",
            category="Барбершоп",
            address="ул. Ленина, 5",
            phone="+7 900 123-45-67",
            working_hours="пн–сб 10:00–21:00, вс — выходной",
            description="Мужские стрижки и уход за бородой в центре города.",
            ai_rules="Отвечай коротко и вежливо. Не обсуждай скидки — только цены из прайса.",
            escalation_contact="Администратор Ирина, +7 900 123-45-00",
            ai_tone=AiTone.FRIENDLY,
        )
        db.add(business)
        db.flush()
        # Как при POST /businesses: у новой компании пробная подписка (этапы 6, 8).
        db.add(admin_service.new_trial_subscription(business.id))
        db.add_all(
            [
                BusinessMember(business_id=business.id, user_id=owner.id, role=MemberRole.OWNER),
                BusinessMember(
                    business_id=business.id, user_id=manager.id, role=MemberRole.MANAGER
                ),
            ]
        )

        for name, price, duration, description, active in [
            ("Стрижка", "1500", 60, "Мужская стрижка, мытьё головы и укладка.", True),
            ("Борода", "1000", 30, "Моделирование бороды и горячее полотенце.", True),
            (
                "Стрижка и борода",
                "2300",
                90,
                "Комплекс со скидкой относительно отдельных услуг.",
                True,
            ),
            ("Детская стрижка", "900", 40, "Для детей до 12 лет.", True),
            ("Камуфляж седины", "1200", 45, None, True),
            ("Укладка", "700", 20, "Временно не оказываем.", False),
        ]:
            db.add(
                Service(
                    business_id=business.id,
                    name=name,
                    price=Decimal(price),
                    duration=duration,
                    description=description,
                    active=active,
                )
            )
        db.flush()

        now = utcnow()
        counter = {"ext": 1000}

        def ago(**kwargs) -> datetime:
            return now - timedelta(**kwargs)

        def conversation(
            person: tuple[str, str],
            script: list[tuple],
            *,
            status: ConversationStatus,
            priority: LeadPriority,
            attention: str | None = None,
            handled: bool = False,
            lead: tuple[LeadStatus, str, str, int | None] | None = None,
            blocked: bool = False,
        ) -> None:
            """script: (sender, text, когда, intent|None, extra) — extra: dict(delivery, decision, reason)."""
            name, username = person
            counter["ext"] += 1
            customer = Customer(
                business_id=business.id,
                channel=Channel.TELEGRAM,
                external_id=str(counter["ext"]),
                name=name,
                username=username,
                channel_blocked=blocked,
                created_at=script[0][2],
            )
            db.add(customer)
            db.flush()
            last_time = script[-1][2]
            conv = Conversation(
                business_id=business.id,
                customer_id=customer.id,
                channel=Channel.TELEGRAM,
                status=status,
                priority=priority,
                attention_reason=attention,
                handled_by_manager=handled,
                created_at=script[0][2],
                updated_at=last_time,
            )
            db.add(conv)
            db.flush()

            last_customer: Message | None = None
            for sender, text, when, intent, extra in script:
                counter["ext"] += 1
                msg = Message(
                    business_id=business.id,
                    conversation_id=conv.id,
                    sender_type=sender,
                    external_message_id=str(counter["ext"]),
                    text=text,
                    intent=intent,
                    created_at=when,
                )
                if sender is SenderType.CUSTOMER:
                    msg.processing_status = ProcessingStatus.DONE
                    msg.processing_attempts = 1
                else:
                    msg.delivery_status = extra.get("delivery", DeliveryStatus.SENT)
                    msg.delivery_attempts = 1
                    if msg.delivery_status is DeliveryStatus.FAILED:
                        msg.delivery_error = "Клиент заблокировал бота"
                    if sender is SenderType.MANAGER:
                        msg.author_user_id = manager.id
                db.add(msg)
                db.flush()
                if sender is SenderType.CUSTOMER:
                    last_customer = msg
                elif sender is SenderType.AI and last_customer is not None:
                    decision = extra.get("decision", "SEND")
                    db.add(
                        AiResponse(
                            business_id=business.id,
                            message_id=last_customer.id,
                            response_message_id=msg.id,
                            model="demo-model",
                            prompt_version="responder-v3",
                            response_text=text,
                            latency_ms=extra.get("latency", 820),
                            status=extra.get("ai_status", AiResponseStatus.SENT),
                            decision=decision,
                            escalation_reason=extra.get("reason"),
                            details={"intent": last_customer.intent, "demo": True},
                            created_at=when,
                        )
                    )
                    last_customer = None

            if lead is not None:
                lead_status, intent, reason, assignee = lead
                db.add(
                    Lead(
                        business_id=business.id,
                        conversation_id=conv.id,
                        status=lead_status,
                        priority=priority,
                        intent=intent,
                        reason=reason,
                        assigned_to=assignee,
                        created_at=script[0][2],
                        updated_at=last_time,
                    )
                )

        C, A, M = SenderType.CUSTOMER, SenderType.AI, SenderType.MANAGER
        sent = {}

        conversation(
            ("Иван Петров", "ivan_petrov"),
            [
                (C, "Здравствуйте! Сколько стоит стрижка и борода?", ago(hours=3), "PRICE", {}),
                (A, PRICE_REPLY, ago(hours=3, minutes=-1), None, sent),
            ],
            status=ConversationStatus.OPEN,
            priority=LeadPriority.WARM,
            lead=(LeadStatus.NEW, "PRICE", "Правила: клиент спрашивает цену услуги", None),
        )
        conversation(
            ("Алексей Смирнов", "alex_smirnov"),
            [
                (
                    C,
                    "Хочу записаться на завтра после 18:00, есть свободное время?",
                    ago(minutes=40),
                    "BOOKING",
                    {},
                ),
                (
                    A,
                    BOOKING_HOLD,
                    ago(minutes=39),
                    None,
                    {
                        "decision": "ESCALATE",
                        "reason": "HOT_LEAD_CONFIRMATION",
                        "ai_status": AiResponseStatus.ESCALATED,
                        "latency": 12,
                    },
                ),
            ],
            status=ConversationStatus.NEEDS_ATTENTION,
            priority=LeadPriority.HOT,
            attention="HOT_LEAD_CONFIRMATION",
            lead=(
                LeadStatus.NEW,
                "BOOKING",
                "Правила: запрос на запись, требуется подтверждение времени человеком",
                None,
            ),
        )
        conversation(
            ("Мария Ковалёва", "maria_k"),
            [
                (
                    C,
                    "Ужасно постригли, недовольна. Верните деньги!",
                    ago(hours=1, minutes=10),
                    "COMPLAINT",
                    {},
                ),
                (
                    A,
                    "Сожалеем, что так вышло. Передали ваше обращение ответственному сотруднику — он свяжется с вами.",
                    ago(hours=1, minutes=9),
                    None,
                    {
                        "decision": "ESCALATE",
                        "reason": "COMPLAINT",
                        "ai_status": AiResponseStatus.ESCALATED,
                        "latency": 9,
                    },
                ),
            ],
            status=ConversationStatus.NEEDS_ATTENTION,
            priority=LeadPriority.HOT,
            attention="COMPLAINT",
            lead=(LeadStatus.NEW, "COMPLAINT", "Правила: в сообщении признаки жалобы", None),
        )
        conversation(
            ("Дмитрий Воронов", "dmitry_v"),
            [
                (C, "Можно записаться в субботу на 12:00?", ago(hours=5), "BOOKING", {}),
                (
                    A,
                    BOOKING_HOLD,
                    ago(hours=5, minutes=-1),
                    None,
                    {
                        "decision": "ESCALATE",
                        "reason": "HOT_LEAD_CONFIRMATION",
                        "ai_status": AiResponseStatus.ESCALATED,
                        "latency": 11,
                    },
                ),
                (
                    M,
                    "Здравствуйте, Дмитрий! Суббота, 12:00 свободна — записал вас на стрижку.",
                    ago(hours=4, minutes=30),
                    None,
                    {},
                ),
                (C, "Отлично, спасибо! Приду.", ago(minutes=25), None, {}),
            ],
            status=ConversationStatus.NEEDS_ATTENTION,
            priority=LeadPriority.HOT,
            attention="CUSTOMER_REPLIED",
            handled=True,
            lead=(
                LeadStatus.IN_PROGRESS,
                "BOOKING",
                "Правила: запрос на запись, требуется подтверждение времени человеком",
                manager.id,
            ),
        )
        conversation(
            ("Анна Лебедева", "anna_l"),
            [
                (C, "Здравствуйте", ago(hours=6), "OTHER", {}),
                (
                    A,
                    HOLD,
                    ago(hours=6, minutes=-1),
                    None,
                    {
                        "decision": "ESCALATE",
                        "reason": "MISSING_DATA",
                        "ai_status": AiResponseStatus.ESCALATED,
                        "latency": 14,
                    },
                ),
            ],
            status=ConversationStatus.NEEDS_ATTENTION,
            priority=LeadPriority.COLD,
            attention="MISSING_DATA",
            lead=(LeadStatus.NEW, "OTHER", "Правила: явное намерение не определено", None),
        )
        conversation(
            ("Павел Романов", "pavel_r"),
            [
                (C, "Сколько стоит стрижка?", ago(hours=8), "PRICE", {}),
                (
                    A,
                    "Актуальные цены: Стрижка — 1 500 ₽. Подскажите, что вас интересует?",
                    ago(hours=8, minutes=-1),
                    None,
                    {"delivery": DeliveryStatus.FAILED, "ai_status": AiResponseStatus.FAILED},
                ),
            ],
            status=ConversationStatus.NEEDS_ATTENTION,
            priority=LeadPriority.WARM,
            attention="DELIVERY_FAILED",
            lead=(LeadStatus.NEW, "PRICE", "Правила: клиент спрашивает цену услуги", None),
            blocked=True,
        )
        conversation(
            ("Реклама", "promo_bot"),
            [
                (
                    C,
                    "Заработок в интернете без вложений! Пишите, расскажем. t.me/easy_money",
                    ago(hours=2),
                    "SPAM",
                    {},
                )
            ],
            status=ConversationStatus.NEEDS_ATTENTION,
            priority=LeadPriority.COLD,
            attention="SPAM_SUSPECTED",
        )

        # Закрытые обращения прошлых дней — для аналитики и истории клиентов.
        history = [
            (
                ("Олег Никитин", "oleg_n"),
                "Какой у вас график работы?",
                "QUESTION",
                2,
                LeadStatus.RESOLVED,
                LeadPriority.WARM,
            ),
            (
                ("Сергей Козлов", "sergey_k"),
                "Сколько стоит детская стрижка?",
                "PRICE",
                3,
                LeadStatus.RESOLVED,
                LeadPriority.WARM,
            ),
            (
                ("Николай Орлов", "nikolay_o"),
                "Сколько стоит борода?",
                "PRICE",
                5,
                LeadStatus.LOST,
                LeadPriority.WARM,
            ),
            (
                ("Тимур Хасанов", "timur_h"),
                "Стрижка и борода — сколько?",
                "PRICE",
                6,
                LeadStatus.RESOLVED,
                LeadPriority.WARM,
            ),
            (
                ("Роман Белов", "roman_b"),
                "Какая цена на камуфляж седины?",
                "PRICE",
                8,
                LeadStatus.RESOLVED,
                LeadPriority.WARM,
            ),
            (
                ("Егор Мельник", "egor_m"),
                "Сколько стоит стрижка?",
                "PRICE",
                9,
                LeadStatus.RESOLVED,
                LeadPriority.WARM,
            ),
            (
                ("Виктор Лис", "viktor_l"),
                "Цена на стрижку?",
                "PRICE",
                11,
                LeadStatus.RESOLVED,
                LeadPriority.WARM,
            ),
            (
                ("Глеб Сорокин", "gleb_s"),
                "Сколько стоит стрижка и борода?",
                "PRICE",
                12,
                LeadStatus.RESOLVED,
                LeadPriority.WARM,
            ),
        ]
        for person, text, intent, days, lead_status, priority in history:
            when = ago(days=days, hours=2)
            conversation(
                person,
                [
                    (C, text, when, intent, {}),
                    (
                        A,
                        PRICE_REPLY,
                        when + timedelta(seconds=2),
                        None,
                        {"latency": 700 + days * 20},
                    ),
                ],
                status=ConversationStatus.RESOLVED,
                priority=priority,
                lead=(
                    lead_status,
                    intent,
                    "Правила: клиент спрашивает цену услуги",
                    manager.id if days % 2 else None,
                ),
            )

        db.commit()
        print("Готово. Демо-данные созданы.")
        print(f"  Владелец:  {OWNER_EMAIL} / {PASSWORD}")
        print(f"  Менеджер:  {MANAGER_EMAIL} / {PASSWORD}")


if __name__ == "__main__":
    main()

"""
Расписание компании для AI-записи — реализация ai.booking.ScheduleProvider
поверх БД (вне ТЗ, §22 «автоматическая запись»).

Провайдер создаётся на одно сообщение клиента и видит данные только своей
компании (business из интеграции канала, раздел 16). Свободное время — из
booking_service.free_slots, бронь — booking_service.create_booking (атомарно).
Что AI предложил клиенту в прошлый раз, провайдер читает из деталей последнего
решения AI в этом диалоге (ai_responses.details.booking).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai.booking import BookableService, BookingKind, SlotOption
from models import AiResponse, BookingSource, Business, Master, Message, Service
from services import booking_service, master_service, schedule_service


class DbScheduleProvider:
    def __init__(
        self,
        db: Session,
        business: Business,
        *,
        conversation_id: int,
        customer_id: int,
        client_name: str,
    ) -> None:
        self._db = db
        self._business_id = business.id
        self._tz = schedule_service.business_tz(business)
        self._conversation_id = conversation_id
        self._customer_id = customer_id
        self._client_name = client_name
        self.today: date = datetime.now(self._tz).date()

    # -- справочники ---------------------------------------------------------- #
    def _business(self) -> Business:
        return self._db.get_one(Business, self._business_id)

    def _service_rows(self) -> list[Service]:
        rows = self._db.scalars(
            select(Service)
            .where(Service.business_id == self._business_id, Service.active.is_(True))
            .order_by(Service.name)
        ).all()
        return [
            s for s in rows if master_service.masters_for_service(self._db, self._business_id, s.id)
        ]

    def services(self) -> list[BookableService]:
        return [BookableService(id=s.id, name=s.name) for s in self._service_rows()]

    def masters(self) -> list[tuple[int, str]]:
        rows = self._db.scalars(
            select(Master)
            .where(Master.business_id == self._business_id, Master.active.is_(True))
            .order_by(Master.display_name)
        ).all()
        return [(m.id, m.display_name) for m in rows]

    # -- расписание ----------------------------------------------------------- #
    def free_slots(
        self, service_id: int, day_from: date, day_to: date, master_id: int | None = None
    ) -> list[SlotOption]:
        service = self._db.get(Service, service_id)
        if service is None or service.business_id != self._business_id:
            return []
        slots = booking_service.free_slots(
            self._db,
            self._business(),
            service,
            day_from=day_from,
            day_to=day_to,
            master_id=master_id,
        )
        return [
            SlotOption(
                master_id=s.master_id,
                master_name=s.master_name,
                starts_at=s.starts_at,
                local_start=s.local_start,
            )
            for s in slots
        ]

    def hold(self, service_id: int, master_id: int, starts_at: datetime) -> int | None:
        master = self._db.get(Master, master_id)
        service = self._db.get(Service, service_id)
        if master is None or service is None:
            return None
        try:
            booking = booking_service.create_booking(
                self._db,
                self._business(),
                master=master,
                service=service,
                starts_at=starts_at,
                client_name=self._client_name,
                source=BookingSource.AI,
                customer_id=self._customer_id,
                conversation_id=self._conversation_id,
            )
        except booking_service.BookingConflict:
            return None
        return booking.id

    # -- память диалога --------------------------------------------------------- #
    def _last_booking_details(self) -> dict | None:
        details = self._db.scalar(
            select(AiResponse.details)
            .join(Message, Message.id == AiResponse.message_id)
            .where(Message.conversation_id == self._conversation_id)
            .order_by(AiResponse.id.desc())
            .limit(1)
        )
        booking = details.get("booking") if isinstance(details, dict) else None
        return booking if isinstance(booking, dict) else None

    def in_booking_dialog(self) -> bool:
        booking = self._last_booking_details()
        return bool(booking) and booking.get("kind") in (
            BookingKind.OFFER.value,
            BookingKind.ASK_SERVICE.value,
        )

    def last_offer(self) -> tuple[int | None, list[SlotOption]]:
        booking = self._last_booking_details()
        if not booking or booking.get("kind") != BookingKind.OFFER.value:
            return None, []
        names = dict(self.masters())
        options: list[SlotOption] = []
        for item in booking.get("offered") or []:
            try:
                starts = datetime.fromisoformat(item["starts_at"])
                master_id = int(item["master_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if master_id not in names:
                continue
            starts = starts if starts.tzinfo else starts.replace(tzinfo=UTC)
            options.append(
                SlotOption(master_id, names[master_id], starts, starts.astimezone(self._tz))
            )
        service_id = booking.get("service_id")
        return (service_id if isinstance(service_id, int) else None), options


def for_conversation(
    db: Session, business: Business, *, conversation_id: int, customer_id: int, client_name: str
) -> DbScheduleProvider | None:
    """Провайдер, если AI-запись включена и есть активные мастера; иначе None."""
    if not business.booking_enabled:
        return None
    has_master = db.scalar(
        select(Master.id).where(Master.business_id == business.id, Master.active.is_(True)).limit(1)
    )
    if has_master is None:
        return None
    return DbScheduleProvider(
        db,
        business,
        conversation_id=conversation_id,
        customer_id=customer_id,
        client_name=client_name,
    )

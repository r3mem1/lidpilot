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

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai.booking import (
    BookableService,
    BookingKind,
    BookingRequest,
    ClientBooking,
    MasterWindows,
    SlotOption,
)
from models import AiResponse, Booking, BookingSource, Business, Master, Message, Service
from services import audit_service, booking_service, master_service, schedule_service


class DbScheduleProvider:
    def __init__(
        self,
        db: Session,
        business: Business,
        *,
        conversation_id: int | None,
        customer_id: int | None,
        client_name: str,
        dry_run: bool = False,
    ) -> None:
        self._db = db
        # «Проверка ответа» в кабинете: бронь не ставится, только проверяется,
        # что окно действительно свободно (ничего в БД не пишется).
        self._dry_run = dry_run
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

    def free_windows(
        self,
        day: date,
        service_id: int | None = None,
        master_id: int | None = None,
        time_from: time | None = None,
        time_to: time | None = None,
    ) -> list[MasterWindows]:
        service = self._db.get(Service, service_id) if service_id else None
        if service is not None and service.business_id != self._business_id:
            return []
        rows = booking_service.free_windows(
            self._db,
            self._business(),
            day,
            service=service,
            master_id=master_id,
            time_from=time_from,
            time_to=time_to,
        )
        return [
            MasterWindows(
                master_id=r.master_id,
                master_name=r.master_name,
                windows=tuple((s.time(), e.time()) for s, e in r.windows),
            )
            for r in rows
        ]

    def hold(self, service_id: int, master_id: int, starts_at: datetime) -> int | None:
        master = self._db.get(Master, master_id)
        service = self._db.get(Service, service_id)
        if master is None or service is None:
            return None
        if self._dry_run:
            local_day = starts_at.astimezone(self._tz).date()
            free = booking_service.free_slots(
                self._db,
                self._business(),
                service,
                day_from=local_day,
                day_to=local_day,
                master_id=master_id,
            )
            return 0 if any(slot.starts_at == starts_at for slot in free) else None
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

    def master_load(self, day: date) -> dict[int, int]:
        """Активные записи (ждут подтверждения и подтверждённые) каждого мастера
        компании за день в её поясе — для выбора мастера на «без разницы»."""
        start = datetime.combine(day, time.min, tzinfo=self._tz).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=self._tz).astimezone(UTC)
        rows = self._db.execute(
            select(Booking.master_id, func.count(Booking.id))
            .where(
                Booking.business_id == self._business_id,
                Booking.status.in_(booking_service.ACTIVE),
                Booking.starts_at >= start,
                Booking.starts_at < end,
            )
            .group_by(Booking.master_id)
        ).all()
        return {master_id: count for master_id, count in rows}

    # -- память диалога --------------------------------------------------------- #
    def _last_booking_details(self) -> dict | None:
        if self._conversation_id is None:  # проверка ответа — без истории диалога
            return None
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
            BookingKind.WINDOWS.value,
            BookingKind.CHANGE_CHOOSE.value,
            BookingKind.CANCEL_ASK.value,
            BookingKind.MOVE_ASK.value,
        )

    # -- запись клиента: отмена и перенос в чате (решение 2026-10-01) -------- #
    def _client_booking_rows(self) -> list[Booking]:
        if self._customer_id is None:
            return []
        return list(
            self._db.scalars(
                select(Booking)
                .where(
                    Booking.business_id == self._business_id,
                    Booking.customer_id == self._customer_id,
                    Booking.status.in_(booking_service.ACTIVE),
                    Booking.starts_at > datetime.now(UTC),
                )
                .order_by(Booking.starts_at)
                .limit(5)
            )
        )

    def client_bookings(self) -> list[ClientBooking]:
        names = dict(self.masters())
        services = {
            s.id: s.name
            for s in self._db.scalars(
                select(Service).where(Service.business_id == self._business_id)
            )
        }
        result = []
        for b in self._client_booking_rows():
            starts = b.starts_at if b.starts_at.tzinfo else b.starts_at.replace(tzinfo=UTC)
            result.append(
                ClientBooking(
                    id=b.id,
                    service_id=b.service_id,
                    service_name=services.get(b.service_id or 0, "услуга"),
                    master_id=b.master_id,
                    master_name=names.get(b.master_id, "—"),
                    local_start=starts.astimezone(self._tz).replace(tzinfo=None),
                )
            )
        return result

    def last_change(self) -> tuple[BookingKind, str, int | None, list[int], date | None] | None:
        booking = self._last_booking_details()
        if not booking or booking.get("kind") not in (
            BookingKind.CHANGE_CHOOSE.value,
            BookingKind.CANCEL_ASK.value,
            BookingKind.MOVE_ASK.value,
        ):
            return None
        change = booking.get("change")
        if not isinstance(change, dict) or change.get("action") not in ("cancel", "move"):
            return None
        booking_id = change.get("booking_id")
        options = [o for o in change.get("options") or [] if isinstance(o, int)]
        ctx = booking.get("context")
        day = None
        if isinstance(ctx, dict) and ctx.get("day"):
            try:
                day = date.fromisoformat(ctx["day"])
            except (TypeError, ValueError):
                day = None
        return (
            BookingKind(booking["kind"]),
            change["action"],
            booking_id if isinstance(booking_id, int) else None,
            options,
            day if day and day >= self.today else None,
        )

    def _own_booking(self, booking_id: int) -> Booking | None:
        """Запись только этого клиента и этой компании (раздел 16)."""
        return next((b for b in self._client_booking_rows() if b.id == booking_id), None)

    def cancel_booking(self, booking_id: int) -> bool:
        booking = self._own_booking(booking_id)
        if booking is None:
            return False
        if self._dry_run:
            return True
        try:
            booking_service.cancel_by_client(self._db, self._business(), booking)
        except booking_service.BookingConflict:
            return False
        return True

    def move_booking(self, booking_id: int, master_id: int, day: date, at: time) -> bool:
        booking = self._own_booking(booking_id)
        master = self._db.get(Master, master_id)
        if booking is None or master is None or master.business_id != self._business_id:
            return False
        starts_at = schedule_service.local_to_utc(self._business(), day, at)
        if self._dry_run:
            service = self._db.get(Service, booking.service_id) if booking.service_id else None
            if service is None:
                return False
            free = booking_service.free_slots(
                self._db, self._business(), service, day_from=day, day_to=day, master_id=master_id
            )
            return any(slot.starts_at == starts_at for slot in free)
        try:
            booking_service.move_by_client(self._db, self._business(), booking, master, starts_at)
        except booking_service.BookingConflict:
            return False
        return True

    def confirm_visit(self, booking_id: int) -> bool:
        booking = self._own_booking(booking_id)
        if booking is None:
            return False
        if self._dry_run:
            return True
        booking.client_confirmed_at = datetime.now(UTC)
        audit_service.log_event(
            self._db,
            event_type=audit_service.EventType.BOOKING_VISIT_CONFIRMED,
            message=f"Клиент подтвердил визит по записи #{booking.id}",
            business_id=self._business_id,
            payload={"booking_id": booking.id},
        )
        self._db.commit()
        return True

    def last_context(self) -> tuple[BookingKind, BookingRequest, list[int]] | None:
        """Сказанное клиентом до списка окон / вопроса об услуге. Каждое поле
        перепроверяется по текущим данным компании; прошедший день отбрасывается."""
        booking = self._last_booking_details()
        if not booking or booking.get("kind") not in (
            BookingKind.WINDOWS.value,
            BookingKind.ASK_SERVICE.value,
        ):
            return None
        ctx = booking.get("context")
        if not isinstance(ctx, dict):
            return None
        service_id = ctx.get("service_id")
        master_id = ctx.get("master_id")
        day = at = None
        try:
            day = date.fromisoformat(ctx["day"]) if ctx.get("day") else None
            at = time.fromisoformat(ctx["at"]) if ctx.get("at") else None
        except (TypeError, ValueError):
            day = at = None
        part = ctx.get("part_of_day")
        service_ids = {s.id for s in self.services()}
        # Номера услуг сохраняют позиции: снятая с записи услуга не сдвигает остальные.
        options = [
            sid if sid in service_ids else 0
            for sid in booking.get("service_options") or []
            if isinstance(sid, int)
        ]
        return (
            BookingKind(booking["kind"]),
            BookingRequest(
                service_id=service_id if service_id in service_ids else None,
                master_id=master_id if master_id in dict(self.masters()) else None,
                day=day if day and day >= self.today else None,
                at=at,
                part_of_day=part if part in ("morning", "day", "evening") else None,
            ),
            options,
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


def _booking_ready(db: Session, business: Business) -> bool:
    if not business.booking_enabled:
        return False
    has_master = db.scalar(
        select(Master.id).where(Master.business_id == business.id, Master.active.is_(True)).limit(1)
    )
    return has_master is not None


def for_preview(db: Session, business: Business) -> DbScheduleProvider | None:
    """Расписание для «Проверки ответа»: те же окна, что увидит клиент, но бронь
    не создаётся (dry_run) — в кабинете виден ответ «Готово, вы записаны»."""
    if not _booking_ready(db, business):
        return None
    return DbScheduleProvider(
        db, business, conversation_id=None, customer_id=None, client_name="Проверка", dry_run=True
    )


def for_conversation(
    db: Session, business: Business, *, conversation_id: int, customer_id: int, client_name: str
) -> DbScheduleProvider | None:
    """Провайдер, если AI-запись включена и есть активные мастера; иначе None."""
    if not _booking_ready(db, business):
        return None
    return DbScheduleProvider(
        db,
        business,
        conversation_id=conversation_id,
        customer_id=customer_id,
        client_name=client_name,
    )

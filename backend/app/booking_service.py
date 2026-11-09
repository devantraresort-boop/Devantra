from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Booking, Room, RoomAvailability
from backend.app.inventory import occupancy_peak


def resort_today() -> date:
    from backend.app.config import settings
    return datetime.now(ZoneInfo(settings.resort_timezone)).date()


def active_bookings(now: datetime):
    return ((Booking.status == "confirmed") | (
        (Booking.status == "pending_payment") & (Booking.expires_at > now)
    ))




def room_quotes(db: Session, rooms: list[Room], check_in: date, check_out: date,
                now: datetime | None = None) -> dict[int, Decimal]:
    """Two queries for all rooms and nights, rather than one query per room/night."""
    if not rooms:
        return {}
    ids = [room.id for room in rooms]
    calendars = defaultdict(list)
    for row in db.scalars(select(RoomAvailability).where(
        RoomAvailability.room_id.in_(ids), RoomAvailability.stay_date >= check_in,
        RoomAvailability.stay_date < check_out,
    )):
        calendars[row.room_id].append(row)
    stays = defaultdict(list)
    for room_id, start, end in db.execute(select(Booking.room_id, Booking.check_in, Booking.check_out).where(
        Booking.room_id.in_(ids), Booking.check_in < check_out, Booking.check_out > check_in,
        active_bookings(now or datetime.utcnow()),
    )):
        stays[room_id].append((start, end))
    quotes = {}
    for room in rooms:
        calendar = calendars[room.id]
        if any(row.is_closed for row in calendar):
            continue
        if occupancy_peak(stays[room.id], check_in, check_out) >= room.inventory:
            continue
        total = room.base_rate * (check_out - check_in).days
        total += sum((row.price_override - room.base_rate for row in calendar
                      if row.price_override is not None), Decimal("0.00"))
        quotes[room.id] = total.quantize(Decimal("0.01"))
    return quotes


def is_available(db, room, check_in, check_out, now=None):
    return room.id in room_quotes(db, [room], check_in, check_out, now)


def quote_total(db, room, check_in, check_out):
    total = room.base_rate * (check_out - check_in).days
    for override in db.scalars(select(RoomAvailability.price_override).where(
        RoomAvailability.room_id == room.id, RoomAvailability.stay_date >= check_in,
        RoomAvailability.stay_date < check_out, RoomAvailability.price_override.is_not(None),
    )):
        total += override - room.base_rate
    return total.quantize(Decimal("0.01"))


def expire_booking(booking: Booking) -> bool:
    if booking.status == "pending_payment" and booking.expires_at and booking.expires_at <= datetime.utcnow():
        booking.status = "expired"
        return True
    return False

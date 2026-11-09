"""Real SQLite/SQLAlchemy tests runnable without the HTTP framework installed."""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.base import Base
from backend.app.booking_service import expire_booking, room_quotes
from backend.app.models import Booking, EmailOutbox, Room, RoomAvailability, RoomCategory


@pytest.fixture
def inventory_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'inventory.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        category = RoomCategory(name="Test category")
        db.add(category)
        db.flush()
        room = Room(category_id=category.id, name="Test room", base_rate=Decimal("5000"),
                    capacity_adults=2, capacity_children=1, inventory=2)
        db.add(room)
        db.commit()
        yield db, room
    engine.dispose()


def booking(room, reference, start, end, **extra):
    values = dict(booking_ref=reference * 32, room_id=room.id, guest_name="Guest", guest_email="guest@example.com",
                  guest_phone="123456789", check_in=start, check_out=end, adults=1, children=0,
                  nights=(end-start).days, nightly_rate=5000, total_amount=5000 * (end-start).days,
                  status="confirmed", payment_status="paid")
    return Booking(**{**values, **extra})


def test_prices_closures_and_expired_holds(inventory_db):
    db, room = inventory_db
    start, end = date(2030, 1, 1), date(2030, 1, 3)
    db.add(RoomAvailability(room_id=room.id, stay_date=start, price_override=Decimal("7200")))
    db.add(booking(room, "a", start, end))
    expired = booking(room, "b", start, end, status="pending_payment", expires_at=datetime.utcnow()-timedelta(minutes=1))
    db.add(expired)
    db.commit()
    assert room_quotes(db, [room], start, end) == {room.id: Decimal("12200")}
    expired.expires_at = datetime.utcnow() + timedelta(minutes=15)
    db.commit()
    assert room_quotes(db, [room], start, end) == {}
    expired.status = "cancelled"
    db.add(RoomAvailability(room_id=room.id, stay_date=start + timedelta(days=1), is_closed=True))
    db.commit()
    assert room_quotes(db, [room], start, end) == {}


def test_inventory_counts_each_night_not_all_overlapping_bookings(inventory_db):
    db, room = inventory_db
    start, middle, end = date(2030, 1, 1), date(2030, 1, 2), date(2030, 1, 3)
    db.add_all([booking(room, "a", start, middle), booking(room, "b", middle, end)])
    db.commit()
    assert room_quotes(db, [room], start, end) == {room.id: Decimal("10000")}


def test_bulk_availability_uses_two_queries_for_many_rooms_and_nights(inventory_db):
    db, first = inventory_db
    rooms = [first]
    for i in range(20):
        rooms.append(Room(category_id=first.category_id, name=f"Room {i}", base_rate=1000, capacity_adults=2, inventory=1))
    db.add_all(rooms)
    db.commit()
    statements = []
    def record(_conn, _cursor, statement, *_): statements.append(statement)
    event.listen(db.bind, "before_cursor_execute", record)
    try:
        start = date(2030, 1, 1)
        quotes = room_quotes(db, rooms, start, start + timedelta(days=365))
        assert len(quotes) == 21
        assert quotes[first.id] == Decimal("1825000")
        assert len(statements) == 2
    finally:
        event.remove(db.bind, "before_cursor_execute", record)


def test_booking_idempotency_unique_constraint(inventory_db):
    db, room = inventory_db
    start, end = date(2030, 1, 1), date(2030, 1, 2)
    db.add(booking(room, "a", start, end, idempotency_key="same-key"))
    db.commit()
    db.add(booking(room, "b", start, end, idempotency_key="same-key"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert len(db.scalars(select(Booking)).all()) == 1


def test_outbox_and_booking_roll_back_together(inventory_db):
    db, room = inventory_db
    db.add(booking(room, "a", date(2030, 1, 1), date(2030, 1, 2)))
    db.add(EmailOutbox(recipient="guest@example.com", subject="Booking", body_html="Stay", body_text="Stay"))
    db.flush()
    db.rollback()
    assert db.scalar(select(Booking)) is None
    assert db.scalar(select(EmailOutbox)) is None


def test_expiration_preserves_confirmed_or_paid_records(inventory_db):
    db, room = inventory_db
    row = booking(room, "a", date(2030, 1, 1), date(2030, 1, 2),
                  status="pending_payment", expires_at=datetime.utcnow()-timedelta(seconds=1))
    assert expire_booking(row)
    assert row.status == "expired"
    row.status = "confirmed"
    assert not expire_booking(row)
    assert row.payment_status == "paid"


def test_postgres_lock_targets_do_not_lock_nullable_joins():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import joinedload
    from backend.app.models import Payment
    statement = select(Payment).options(joinedload(Payment.booking)).with_for_update(of=Payment)
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert sql.endswith("FOR UPDATE OF payments")

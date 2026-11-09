import hashlib
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from backend.app.booking_service import expire_booking, resort_today, room_quotes
from backend.app.config import settings
from backend.app.database import get_db
from backend.app.email_service import booking_email, booking_status_email
from backend.app.limiter import limiter
from backend.app.models import Booking, BookingGuest, Room
from backend.app.outbox import enqueue_email
from backend.app.schemas import BookingCreate, BookingLookup, BookingOut

router = APIRouter(prefix="/api/bookings", tags=["bookings"])


def _out(booking: Booking) -> dict:
    effective_status = booking.status
    if effective_status == "pending_payment" and booking.expires_at and booking.expires_at <= datetime.utcnow():
        effective_status = "expired"
    return {
        "booking_ref": booking.booking_ref, "room_id": booking.room_id, "room_name": booking.room.name,
        "guest_name": booking.guest_name, "guest_email": booking.guest_email,
        "check_in": booking.check_in, "check_out": booking.check_out, "adults": booking.adults,
        "children": booking.children, "nights": booking.nights, "nightly_rate": booking.nightly_rate,
        "total_amount": booking.total_amount, "status": effective_status,
        "payment_status": booking.payment_status, "expires_at": booking.expires_at,
    }


@router.post("", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
def create_booking(request: Request, payload: BookingCreate, db: Session = Depends(get_db),
                   idempotency_key: str | None = Header(default=None, min_length=16, max_length=128)):
    if payload.check_in < resort_today():
        raise HTTPException(status_code=422, detail="Check-in cannot be in the past.")
    # All inventory changes acquire the same parent room lock first.
    room = db.scalar(select(Room).where(Room.id == payload.room_id).with_for_update(of=Room))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found.")
    payload.guest_email = str(payload.guest_email).lower()
    request_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    if idempotency_key:
        existing = db.scalar(select(Booking).where(Booking.idempotency_key == idempotency_key))
        if existing:
            if existing.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="This request key was already used for different booking details.")
            return _out(existing)
    if not room.is_active:
        raise HTTPException(status_code=404, detail="Room not found.")
    if payload.adults > room.capacity_adults or payload.children > room.capacity_children:
        raise HTTPException(status_code=422, detail="Guest count exceeds this room's capacity.")
    now = datetime.utcnow()
    amount = room_quotes(db, [room], payload.check_in, payload.check_out, now).get(room.id)
    if amount is None:
        raise HTTPException(status_code=409, detail="This room is no longer available for the selected dates.")
    if amount <= 0:
        raise HTTPException(status_code=409, detail="This stay does not have a valid payable rate. Contact the resort.")
    nights = (payload.check_out - payload.check_in).days
    # Guest checkout does not mutate a user account solely on an unverified email address.
    booking = Booking(
        booking_ref=uuid.uuid4().hex, idempotency_key=idempotency_key, request_hash=request_hash,
        room_id=room.id, guest_name=payload.guest_name, guest_email=str(payload.guest_email),
        guest_phone=payload.guest_phone, check_in=payload.check_in, check_out=payload.check_out,
        adults=payload.adults, children=payload.children, nights=nights,
        nightly_rate=(amount / nights).quantize(Decimal("0.01")), total_amount=amount,
        status="pending_payment", payment_status="pending",
        expires_at=now + timedelta(minutes=settings.booking_hold_minutes),
    )
    booking.guests.append(BookingGuest(name=payload.guest_name, guest_type="adult"))
    db.add(booking)
    html_body, text_body = booking_email(booking.booking_ref, booking.guest_name, room.name,
        booking.check_in.isoformat(), booking.check_out.isoformat(), f"{booking.total_amount:.2f}")
    enqueue_email(db, booking.guest_email, f"Booking request received · {settings.resort_name}",
                  html_body, text_body, dedupe_key=f"booking:{booking.booking_ref}:requested")
    db.commit()
    return _out(booking)


@router.post("/lookup", response_model=BookingOut)
@limiter.limit("20/minute")
def lookup_booking(request: Request, payload: BookingLookup, db: Session = Depends(get_db)):
    return get_booking(payload.booking_ref, str(payload.email), db)


@router.get("/{booking_ref}", response_model=BookingOut)
@limiter.limit("20/minute")
def get_booking_route(request: Request, booking_ref: str,
                      email: str = Query(..., min_length=5, max_length=254), db: Session = Depends(get_db)):
    return get_booking(booking_ref, email, db)


def get_booking(booking_ref: str, email: str, db: Session):
    booking = db.scalar(select(Booking).options(joinedload(Booking.room)).where(
        Booking.booking_ref == booking_ref, Booking.guest_email == email.lower()))
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found.")
    return _out(booking)


@router.post("/cancel")
@limiter.limit("10/minute")
def cancel_by_details(request: Request, payload: BookingLookup, db: Session = Depends(get_db)):
    return _cancel(payload.booking_ref, str(payload.email), db)


@router.post("/{booking_ref}/cancel")
@limiter.limit("10/minute")
def cancel_booking(request: Request, booking_ref: str,
                   email: str = Query(..., min_length=5, max_length=254), db: Session = Depends(get_db)):
    return _cancel(booking_ref, email, db)


def _cancel(booking_ref, email, db):
    booking = db.scalar(select(Booking).where(
        Booking.booking_ref == booking_ref, Booking.guest_email == email.lower(),
    ).with_for_update(of=Booking))
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found.")
    if booking.status == "cancelled":
        return {"booking_ref": booking.booking_ref, "status": "cancelled"}
    if expire_booking(booking):
        db.commit()
        raise HTTPException(status_code=409, detail="This booking hold has expired.")
    if booking.status not in {"pending_payment", "confirmed"} or booking.check_in < resort_today():
        raise HTTPException(status_code=409, detail="Contact the resort to change this booking.")
    booking.status = "cancelled"
    booking.cancellation_reason = "Cancelled by guest."
    html_body, text_body = booking_status_email(booking.guest_name, booking.booking_ref,
        "Your booking was cancelled", "Any captured payment requires separate refund processing by the resort.")
    enqueue_email(db, booking.guest_email, f"Booking cancelled · {settings.resort_name}",
                  html_body, text_body, dedupe_key=f"booking:{booking.booking_ref}:cancelled")
    db.commit()
    return {"booking_ref": booking.booking_ref, "status": booking.status,
            "payment_status": booking.payment_status,
            "message": "Booking cancelled. Any captured payment requires separate refund processing by the resort."}

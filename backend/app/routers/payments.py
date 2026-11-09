import hashlib
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from starlette.concurrency import run_in_threadpool

from backend.app.booking_service import expire_booking
from backend.app.config import settings
from backend.app.database import get_db
from backend.app.email_service import booking_status_email
from backend.app.limiter import limiter
from backend.app.models import Admin, AdminActivityLog, Booking, Payment, PaymentTransaction, Room, WebhookEvent
from backend.app.outbox import enqueue_email
from backend.app.payment_service import compact_json, ensure_razorpay_configured, razorpay_request, to_paise, verify_checkout_signature, verify_webhook_signature
from backend.app.schemas import PaymentOrderRequest, PaymentVerifyRequest, RefundRequest
from backend.app.security import require_admin

router = APIRouter(prefix="/api/payments", tags=["payments"])
admin_router = APIRouter(prefix="/api/admin/payments", tags=["admin payments"])


def lock_booking(db, *, booking_id=None, booking_ref=None):
    condition = Booking.id == booking_id if booking_id is not None else Booking.booking_ref == booking_ref
    room_id = db.scalar(select(Booking.room_id).where(condition))
    if room_id is None:
        raise HTTPException(status_code=404, detail="Booking not found.")
    # Captures and reservations must serialize on the room, including at hold expiry.
    db.scalar(select(Room).where(Room.id == room_id).with_for_update(of=Room))
    return db.scalar(select(Booking).where(condition).with_for_update(of=Booking)
                     .execution_options(populate_existing=True))


def lock_payment(db, payment_id):
    # Every money operation locks Room -> Booking -> Payment in the same order.
    booking_id = db.scalar(select(Payment.booking_id).where(Payment.id == payment_id))
    if booking_id is None:
        raise HTTPException(status_code=404, detail="Payment not found.")
    booking = lock_booking(db, booking_id=booking_id)
    payment = db.scalar(select(Payment).where(Payment.id == payment_id).with_for_update(of=Payment)
                        .execution_options(populate_existing=True))
    return booking, payment


def notify_booking(db, booking, title, details, key):
    html, text = booking_status_email(booking.guest_name, booking.booking_ref, title, details)
    enqueue_email(db, booking.guest_email, f"{title} · {settings.resort_name}", html, text, dedupe_key=key)


def payment_result(booking, payment):
    return {"booking_ref": booking.booking_ref, "booking_status": booking.status,
            "payment_status": payment.status,
            "message": "Booking confirmed." if booking.status == "confirmed" else
            "Payment was received but the stay is not confirmed. Contact the resort for refund assistance."}


def confirm_payment(db, booking, payment, gateway_payment_id, event_id=None):
    if payment.gateway_payment_id:
        if payment.gateway_payment_id != gateway_payment_id:
            raise HTTPException(status_code=409, detail="A different payment is already recorded. Contact the resort.")
        return  # Never reverse refunds or enqueue duplicate confirmation emails.
    expire_booking(booking)
    payment.gateway_payment_id = gateway_payment_id
    payment.status = "captured"
    booking.payment_status = "paid"
    if booking.status == "pending_payment" and booking.expires_at and booking.expires_at > datetime.utcnow():
        booking.status = "confirmed"
        booking.expires_at = None
        title = "Your booking is confirmed"
        details = (f"{booking.room.name}, {booking.check_in} to {booking.check_out}. "
                   f"Payment of INR {payment.amount:.2f} was verified.")
    else:
        title = "Payment received — please contact the resort"
        details = "Your hold ended or the booking was cancelled. The stay is not confirmed; contact the resort for refund assistance."
    db.add(PaymentTransaction(payment_id=payment.id, gateway_event_id=event_id,
        gateway_payment_id=gateway_payment_id, event_type="payment.captured", status="captured",
        amount=payment.amount, payload="{}"))
    notify_booking(db, booking, title, details, f"payment:{payment.id}:captured")


@router.post("/create-order")
@limiter.limit("15/minute")
def create_order(request: Request, payload: PaymentOrderRequest, db: Session = Depends(get_db)):
    ensure_razorpay_configured()
    booking = lock_booking(db, booking_ref=payload.booking_ref)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found.")
    if expire_booking(booking):
        db.commit()
        raise HTTPException(status_code=410, detail="This booking hold expired. Check availability again.")
    if booking.status != "pending_payment" or booking.payment_status != "pending":
        raise HTTPException(status_code=409, detail="This booking is not awaiting payment.")
    payment = db.scalar(select(Payment).where(Payment.booking_id == booking.id).with_for_update(of=Payment))
    if payment is None:
        payment = Payment(payment_ref=uuid.uuid4().hex, booking_id=booking.id,
                          amount=booking.total_amount, currency="INR", status="pending")
        db.add(payment)
        db.flush()
    if not payment.gateway_order_id:
        order = razorpay_request("POST", "/orders", json={"amount": to_paise(payment.amount),
            "currency": payment.currency, "receipt": booking.booking_ref,
            "notes": {"booking_ref": booking.booking_ref}})
        if not isinstance(order.get("id"), str) or order.get("amount") != to_paise(payment.amount) or order.get("currency") != payment.currency:
            raise HTTPException(status_code=502, detail="Payment provider returned an invalid order.")
        payment.gateway_order_id = order["id"]
        payment.status = "created"
        db.add(PaymentTransaction(payment_id=payment.id, event_type="order.created", status="created",
                                  amount=payment.amount, payload=compact_json({"order_id": order["id"]})))
    db.commit()
    return {"key_id": settings.razorpay_key_id, "order_id": payment.gateway_order_id,
            "payment_ref": payment.payment_ref, "amount": to_paise(payment.amount), "currency": payment.currency,
            "name": settings.resort_name, "description": f"{booking.room.name} · {booking.nights} nights",
            "prefill": {"name": booking.guest_name, "email": booking.guest_email, "contact": booking.guest_phone}}


def check_gateway_payment(entity, payment, payment_id):
    if (entity.get("id") != payment_id or entity.get("order_id") != payment.gateway_order_id
            or entity.get("amount") != to_paise(payment.amount) or entity.get("currency") != payment.currency):
        raise HTTPException(status_code=400, detail="Payment details do not match the recorded order.")


@router.post("/verify")
@limiter.limit("20/minute")
def verify_payment(request: Request, payload: PaymentVerifyRequest, db: Session = Depends(get_db)):
    ensure_razorpay_configured()
    booking = lock_booking(db, booking_ref=payload.booking_ref)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found.")
    payment = db.scalar(select(Payment).where(Payment.booking_id == booking.id).with_for_update(of=Payment))
    if payment is None or payment.gateway_order_id != payload.razorpay_order_id:
        raise HTTPException(status_code=400, detail="Payment order does not match this booking.")
    if not verify_checkout_signature(payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature):
        raise HTTPException(status_code=400, detail="Payment signature verification failed.")
    if payment.gateway_payment_id:
        if payment.gateway_payment_id != payload.razorpay_payment_id:
            raise HTTPException(status_code=409, detail="A different payment is already recorded.")
        return payment_result(booking, payment)
    entity = razorpay_request("GET", f"/payments/{payload.razorpay_payment_id}")
    check_gateway_payment(entity, payment, payload.razorpay_payment_id)
    if entity.get("status") == "authorized":
        entity = razorpay_request("POST", f"/payments/{payload.razorpay_payment_id}/capture",
                                 json={"amount": to_paise(payment.amount), "currency": payment.currency})
        check_gateway_payment(entity, payment, payload.razorpay_payment_id)
    if entity.get("status") != "captured":
        raise HTTPException(status_code=409, detail="The gateway has not confirmed a captured payment. Retry verification shortly.")
    confirm_payment(db, booking, payment, payload.razorpay_payment_id)
    db.commit()
    return payment_result(booking, payment)


@router.get("/{payment_ref}/status")
def payment_status(payment_ref: str, db: Session = Depends(get_db)):
    payment = db.scalar(select(Payment).options(joinedload(Payment.booking)).where(Payment.payment_ref == payment_ref))
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found.")
    return {"payment_ref": payment.payment_ref, "status": payment.status,
            "booking_ref": payment.booking.booking_ref,
            "booking_status": "expired" if (payment.booking.status == "pending_payment" and
                payment.booking.expires_at and payment.booking.expires_at <= datetime.utcnow()) else payment.booking.status}


def apply_refund(db, booking, payment, transaction, entity):
    if (entity.get("payment_id") != payment.gateway_payment_id or entity.get("amount") != to_paise(transaction.amount)
            or entity.get("currency") != payment.currency or not entity.get("id")):
        raise HTTPException(status_code=400, detail="Refund details do not match the recorded request.")
    if transaction.gateway_refund_id and transaction.gateway_refund_id != entity["id"]:
        raise HTTPException(status_code=409, detail="Refund reference mismatch.")
    transaction.gateway_refund_id = entity["id"]
    if transaction.status == "processed":
        return
    new_status = entity.get("status", "pending")
    if new_status == "processed":
        if payment.refund_amount + transaction.amount > payment.amount:
            raise HTTPException(status_code=409, detail="Refund exceeds the recorded payment.")
        payment.refund_amount += transaction.amount
        payment.status = "refunded" if payment.refund_amount == payment.amount else "partially_refunded"
        booking.payment_status = payment.status
        transaction.status = "processed"
        notify_booking(db, booking, "Refund processed", f"INR {transaction.amount:.2f} was refunded.",
                       f"refund:{transaction.id}:processed")
    elif new_status == "failed":
        transaction.status = "failed"
        payment.status = "captured" if payment.refund_amount == 0 else "partially_refunded"
        booking.payment_status = "paid" if payment.refund_amount == 0 else "partially_refunded"
    else:
        transaction.status = "pending"
        payment.status = booking.payment_status = "refund_pending"


def process_webhook(body, signature, event_id, db):
    if not verify_webhook_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")
    try:
        event = json.loads(body)
        event_type = event["event"]
        payload = event["payload"]
        if not isinstance(payload, dict) or not isinstance(event_type, str):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400, detail="Invalid webhook payload.") from None
    if event_type not in {"payment.captured", "order.paid", "payment.failed", "refund.processed", "refund.failed"}:
        return {"received": True, "processed": False}
    event_id = event_id or hashlib.sha256(body).hexdigest()
    is_refund = event_type.startswith("refund.")
    data = payload.get("refund" if is_refund else "payment", {})
    entity = data.get("entity", {}) if isinstance(data, dict) else {}
    if event_type == "order.paid" and not entity:
        return {"received": True, "processed": False}
    if not isinstance(entity, dict) or not isinstance(entity.get("id"), str) or len(entity["id"]) > 120:
        raise HTTPException(status_code=400, detail="Invalid gateway entity.")
    gateway_ref = entity.get("payment_id" if is_refund else "order_id")
    if not isinstance(gateway_ref, str) or len(gateway_ref) > 120:
        raise HTTPException(status_code=400, detail="Invalid gateway reference.")
    column = Payment.gateway_payment_id if is_refund else Payment.gateway_order_id
    payment_id = db.scalar(select(Payment.id).where(column == gateway_ref))
    if payment_id is None:
        raise HTTPException(status_code=404, detail="Payment not found.")
    booking, payment = lock_payment(db, payment_id)
    # Check after acquiring the lock: simultaneous deliveries are serialized.
    if db.get(WebhookEvent, event_id):
        return {"received": True, "processed": True}
    if is_refund:
        transaction = db.scalar(select(PaymentTransaction).where(
            PaymentTransaction.payment_id == payment.id, PaymentTransaction.gateway_refund_id == entity["id"]))
        if transaction is None and entity.get("receipt"):
            transaction = db.scalar(select(PaymentTransaction).where(
                PaymentTransaction.payment_id == payment.id, PaymentTransaction.request_key == entity["receipt"],
                PaymentTransaction.event_type == "refund.requested"))
        if transaction is None:
            raise HTTPException(status_code=404, detail="Refund request not found; retry after reconciliation.")
        apply_refund(db, booking, payment, transaction, {**entity, "status": event_type.split(".")[1]})
    else:
        check_gateway_payment(entity, payment, entity["id"])
        if event_type == "payment.failed":
            if not payment.gateway_payment_id:
                payment.status = "failed"
                db.add(PaymentTransaction(payment_id=payment.id, gateway_event_id=event_id,
                    gateway_payment_id=entity["id"], event_type=event_type, status="failed", amount=payment.amount, payload="{}"))
        else:
            confirm_payment(db, booking, payment, entity["id"], event_id)
    db.add(WebhookEvent(event_id=event_id, event_type=event_type))
    db.commit()
    return {"received": True, "processed": True}


@router.post("/webhook")
@limiter.exempt
async def payment_webhook(request: Request, db: Session = Depends(get_db),
    x_razorpay_signature: str | None = Header(default=None, max_length=256),
    x_razorpay_event_id: str | None = Header(default=None, max_length=128)):
    body = await request.body()
    # SQLAlchemy is synchronous: keep it off the async event loop.
    return await run_in_threadpool(process_webhook, body, x_razorpay_signature, x_razorpay_event_id, db)


@admin_router.get("")
def list_payments(limit: int = Query(default=100, ge=1, le=250), offset: int = Query(default=0, ge=0),
                  db: Session = Depends(get_db), _admin: Admin = Depends(require_admin)):
    rows = db.scalars(select(Payment).options(joinedload(Payment.booking))
        .order_by(Payment.created_at.desc(), Payment.id.desc()).offset(offset).limit(limit)).all()
    return [{"payment_ref": row.payment_ref, "booking_ref": row.booking.booking_ref,
        "guest_name": row.booking.guest_name, "amount": row.amount, "status": row.status,
        "refund_amount": row.refund_amount, "created_at": row.created_at} for row in rows]


@admin_router.post("/{payment_ref}/refund")
def refund_payment(payment_ref: str, payload: RefundRequest, db: Session = Depends(get_db),
                   admin: Admin = Depends(require_admin),
                   idempotency_key: str | None = Header(default=None, min_length=16, max_length=128)):
    ensure_razorpay_configured()
    payment_id = db.scalar(select(Payment.id).where(Payment.payment_ref == payment_ref))
    booking, payment = lock_payment(db, payment_id)
    # A short gateway receipt doubles as a stable refund retry key.
    key = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32] if idempotency_key else uuid.uuid4().hex
    existing = db.scalar(select(PaymentTransaction).where(PaymentTransaction.request_key == key))
    if existing:
        if existing.payment_id != payment.id or (payload.amount is not None and existing.amount != payload.amount):
            raise HTTPException(status_code=409, detail="Refund request key was used with different details.")
        return {"refund_id": existing.gateway_refund_id, "status": existing.status, "amount": existing.amount}
    if payment.status not in {"captured", "partially_refunded"} or not payment.gateway_payment_id:
        raise HTTPException(status_code=409, detail="Only captured payments without an outstanding refund can be refunded.")
    amount = payload.amount if payload.amount is not None else payment.amount - payment.refund_amount
    if amount <= 0 or amount + payment.refund_amount > payment.amount:
        raise HTTPException(status_code=422, detail="Refund exceeds the remaining captured amount.")
    transaction = PaymentTransaction(payment_id=payment.id, gateway_payment_id=payment.gateway_payment_id,
        event_type="refund.requested", status="submitted", amount=amount, request_key=key,
        payload=compact_json({"reason": payload.reason}))
    db.add(transaction)
    payment.status = booking.payment_status = "refund_pending"
    db.add(AdminActivityLog(admin_id=admin.id, action="payment.refund", entity_type="payment",
                           entity_id=payment.payment_ref, details=f"Refund request {key}"))
    gateway_payment_id = payment.gateway_payment_id
    db.commit()  # Persist intent before contacting the provider; never blindly resubmit after a timeout.
    refund = razorpay_request("POST", f"/payments/{gateway_payment_id}/refund",
        headers={"X-Refund-Idempotency": key},
        json={"amount": to_paise(amount), "receipt": key, "notes": {"reason": payload.reason}})
    booking, payment = lock_payment(db, payment_id)
    transaction = db.scalar(select(PaymentTransaction).where(PaymentTransaction.request_key == key)
                            .execution_options(populate_existing=True))
    apply_refund(db, booking, payment, transaction, refund)
    db.commit()
    return {"refund_id": transaction.gateway_refund_id, "status": transaction.status, "amount": amount}


@admin_router.post("/{payment_ref}/reconcile-refund")
def reconcile_refund(payment_ref: str, db: Session = Depends(get_db), admin: Admin = Depends(require_admin)):
    ensure_razorpay_configured()
    payment_id = db.scalar(select(Payment.id).where(Payment.payment_ref == payment_ref))
    booking, payment = lock_payment(db, payment_id)
    transaction = db.scalar(select(PaymentTransaction).where(PaymentTransaction.payment_id == payment.id,
        PaymentTransaction.event_type == "refund.requested", PaymentTransaction.status.in_(("submitted", "pending")))
        .order_by(PaymentTransaction.id.desc()).limit(1))
    if transaction is None:
        return {"status": payment.status}
    if transaction.gateway_refund_id:
        entity = razorpay_request("GET", f"/refunds/{transaction.gateway_refund_id}")
    else:
        refunds = razorpay_request("GET", f"/payments/{payment.gateway_payment_id}/refunds", params={"count": 100})
        entity = next((row for row in refunds.get("items", []) if row.get("receipt") == transaction.request_key), None)
        if entity is None:
            raise HTTPException(status_code=409, detail="Refund outcome is still unknown. Check Razorpay before creating any new refund.")
    apply_refund(db, booking, payment, transaction, entity)
    db.add(AdminActivityLog(admin_id=admin.id, action="payment.refund_reconcile", entity_type="payment", entity_id=payment_ref))
    db.commit()
    return {"status": transaction.status, "refund_id": transaction.gateway_refund_id}

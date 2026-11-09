import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from backend.app.config import Settings, settings
from backend.app.models import Booking, EmailOutbox, Payment, PaymentTransaction, Room, User, WebhookEvent
from backend.app.routers import payments
from tests.test_booking_api import booking_payload, stay_dates


def login(client):
    result = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "test-password-123"})
    assert result.status_code == 200
    return {"Authorization": f"Bearer {result.json()['access_token']}"}


def reserve(client, room_id, **kwargs):
    start, end = stay_dates()
    response = client.post("/api/bookings", json=booking_payload(room_id, start, end, **kwargs))
    assert response.status_code == 201, response.text
    return response.json()


def signed_event(client, monkeypatch, event, event_id="evt_one"):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-webhook-secret")
    body = json.dumps(event).encode()
    signature = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
    return client.post("/api/payments/webhook", content=body,
        headers={"X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": event_id})


def test_concurrent_idempotent_booking_and_atomic_email(client, sessions, room_id):
    start, end = stay_dates()
    payload = booking_payload(room_id, start, end)
    headers = {"Idempotency-Key": "booking-retry-key-12345"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: client.post("/api/bookings", json=payload, headers=headers), range(2)))
    assert [r.status_code for r in responses] == [201, 201]
    assert responses[0].json()["booking_ref"] == responses[1].json()["booking_ref"]
    with sessions() as db:
        assert db.scalar(select(func.count(Booking.id))) == 1
        assert db.scalar(select(func.count(EmailOutbox.id))) == 1
    changed = client.post("/api/bookings", json={**payload, "guest_name": "Changed Guest"}, headers=headers)
    assert changed.status_code == 409


def test_guest_checkout_does_not_overwrite_existing_user(client, sessions, room_id):
    with sessions() as db:
        db.add(User(name="Verified Owner", email="guest@example.com", phone="123456789"))
        db.commit()
    reserve(client, room_id)
    with sessions() as db:
        assert db.scalar(select(User)).name == "Verified Owner"


def test_booking_lookup_and_expired_status(client, sessions, room_id):
    booking = reserve(client, room_id)
    with sessions() as db:
        row = db.scalar(select(Booking))
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    response = client.post("/api/bookings/lookup", json={"booking_ref": booking["booking_ref"], "email": "guest@example.com"})
    assert response.json()["status"] == "expired"
    assert client.post("/api/bookings/lookup", json={"booking_ref": booking["booking_ref"], "email": "other@example.com"}).status_code == 404


def test_logout_revokes_token_and_password_change(client):
    headers = login(client)
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    headers = login(client)
    assert client.post("/api/auth/password", headers=headers, json={"current_password": "wrong", "new_password": "new-password-12345"}).status_code == 400
    assert client.post("/api/auth/password", headers=headers, json={"current_password": "test-password-123", "new_password": "new-password-12345"}).status_code == 204
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    assert client.post("/api/auth/login", json={"email": "admin@example.com", "password": "new-password-12345"}).status_code == 200


def test_site_info_is_editable_and_used_for_enquiry_notification(client, sessions):
    payload = {"name": "Resort", "phone": "123456789", "email": "desk@example.com", "address": "Verified address", "cancellation_policy": "Contact reception before cancelling."}
    assert client.put("/api/admin/site-info", json=payload).status_code == 401
    assert client.put("/api/admin/site-info", headers=login(client), json=payload).status_code == 200
    assert client.get("/api/site-info").json()["cancellation_policy"] == payload["cancellation_policy"]
    assert client.post("/api/contact", json={"name": "Guest", "email": "guest@example.com", "message": "Please share the rates."}).status_code == 201
    with sessions() as db:
        assert db.scalar(select(EmailOutbox)).recipient == "desk@example.com"


def test_admin_cannot_reduce_reserved_inventory_or_close_dates(client, sessions, room_id):
    with sessions() as db:
        db.get(Room, room_id).inventory = 2
        db.commit()
    reserve(client, room_id)
    reserve(client, room_id, guest_email="other@example.com")
    headers = login(client)
    assert client.put(f"/api/admin/rooms/{room_id}", headers=headers, json={"inventory": 1}).status_code == 409
    start, end = stay_dates()
    assert client.put("/api/admin/availability", headers=headers, json={"room_id": room_id, "start_date": start, "end_date": end, "is_closed": True}).status_code == 409


@pytest.mark.parametrize("change", [{"name": None}, {"base_rate": None}, {"image_url": "javascript:alert(1)"}])
def test_invalid_room_update_is_rejected(client, room_id, change):
    assert client.put(f"/api/admin/rooms/{room_id}", headers=login(client), json=change).status_code == 422


def test_availability_query_count_is_independent_of_stay_length(sessions, room_id):
    from backend.app.booking_service import room_quotes
    from sqlalchemy import event
    with sessions() as db:
        room = db.get(Room, room_id)
        statements = []
        def record(*args): statements.append(args[2])
        event.listen(db.bind, "before_cursor_execute", record)
        try:
            begin = date.today() + timedelta(days=10)
            assert room_quotes(db, [room], begin, begin + timedelta(days=100))[room_id] == Decimal("500000")
            assert len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]) == 2
        finally:
            event.remove(db.bind, "before_cursor_execute", record)


def create_payment(sessions, room_id, *, expired=False):
    start = date.today() + timedelta(days=10)
    with sessions() as db:
        booking = Booking(booking_ref="f" * 32, room_id=room_id, guest_name="Guest", guest_email="guest@example.com",
            guest_phone="123456789", check_in=start, check_out=start + timedelta(days=1), adults=1, children=0,
            nights=1, nightly_rate=5000, total_amount=5000, status="pending_payment", payment_status="pending",
            expires_at=datetime.utcnow() + timedelta(minutes=-1 if expired else 15))
        db.add(booking); db.flush()
        payment = Payment(payment_ref="a" * 32, booking_id=booking.id, gateway_order_id="order_test",
                          amount=5000, currency="INR", status="created")
        db.add(payment); db.commit()
        return payment.id


def capture_event(amount=500000):
    return {"event": "payment.captured", "payload": {"payment": {"entity": {
        "entity": "payment", "id": "pay_test", "order_id": "order_test", "amount": amount, "currency": "INR"}}}}


def test_capture_replay_sends_one_confirmation(client, sessions, room_id, monkeypatch):
    payment_id = create_payment(sessions, room_id)
    assert signed_event(client, monkeypatch, capture_event()).status_code == 200
    assert signed_event(client, monkeypatch, capture_event()).status_code == 200
    assert signed_event(client, monkeypatch, capture_event(), "evt_second").status_code == 200
    with sessions() as db:
        payment = db.get(Payment, payment_id)
        assert payment.booking.status == "confirmed"
        assert db.scalar(select(func.count(EmailOutbox.id))) == 1
        assert db.scalar(select(func.count(WebhookEvent.event_id))) == 2


def test_late_payment_does_not_steal_new_reservation(client, sessions, room_id, monkeypatch):
    payment_id = create_payment(sessions, room_id, expired=True)
    reserve(client, room_id)
    assert signed_event(client, monkeypatch, capture_event()).status_code == 200
    with sessions() as db:
        payment = db.get(Payment, payment_id)
        assert payment.booking.status == "expired"
        assert payment.booking.payment_status == "paid"


def test_invalid_signature_and_amount_do_not_capture(client, sessions, room_id, monkeypatch):
    payment_id = create_payment(sessions, room_id)
    assert client.post("/api/payments/webhook", json=capture_event(), headers={"X-Razorpay-Signature": "invalid"}).status_code == 400
    assert signed_event(client, monkeypatch, capture_event(amount=1)).status_code == 400
    with sessions() as db:
        assert db.get(Payment, payment_id).status == "created"


def test_checkout_verification_and_webhook_do_not_duplicate(client, sessions, room_id, monkeypatch):
    payment_id = create_payment(sessions, room_id)
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_demo")
    monkeypatch.setattr(settings, "razorpay_key_secret", "checkout-secret")
    entity = {**capture_event()["payload"]["payment"]["entity"], "status": "captured"}
    monkeypatch.setattr(payments, "razorpay_request", lambda *a, **k: entity)
    signature = hmac.new(b"checkout-secret", b"order_test|pay_test", hashlib.sha256).hexdigest()
    response = client.post("/api/payments/verify", json={"booking_ref": "f" * 32, "razorpay_order_id": "order_test",
        "razorpay_payment_id": "pay_test", "razorpay_signature": signature})
    assert response.status_code == 200
    assert signed_event(client, monkeypatch, capture_event()).status_code == 200
    with sessions() as db:
        assert db.get(Payment, payment_id).booking.status == "confirmed"
        assert db.scalar(select(func.count(EmailOutbox.id))) == 1


def test_refund_defaults_to_remaining_amount_and_retries_are_idempotent(client, sessions, room_id, monkeypatch):
    payment_id = create_payment(sessions, room_id)
    signed_event(client, monkeypatch, capture_event())
    with sessions() as db:
        payment = db.get(Payment, payment_id)
        payment.refund_amount = Decimal("1000")
        payment.status = "partially_refunded"
        db.commit()
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_demo")
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-secret")
    calls = []
    def gateway(*args, **kwargs):
        calls.append(kwargs)
        return {"id": "rfnd_test", "payment_id": "pay_test", "amount": 400000, "currency": "INR", "status": "processed"}
    monkeypatch.setattr(payments, "razorpay_request", gateway)
    headers = {**login(client), "Idempotency-Key": "refund-request-12345"}
    for _ in range(2):
        response = client.post(f"/api/admin/payments/{'a' * 32}/refund", json={}, headers=headers)
        assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert calls[0]["json"]["amount"] == 400000
    assert "X-Refund-Idempotency" in calls[0]["headers"]
    with sessions() as db:
        assert db.get(Payment, payment_id).refund_amount == Decimal("5000")


def test_refund_timeout_retains_intent_and_blocks_second_request(client, sessions, room_id, monkeypatch):
    from fastapi import HTTPException
    payment_id = create_payment(sessions, room_id)
    signed_event(client, monkeypatch, capture_event())
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_demo")
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-secret")
    def timeout(*a, **k): raise HTTPException(status_code=502, detail="Gateway timeout")
    monkeypatch.setattr(payments, "razorpay_request", timeout)
    headers = login(client)
    path = f"/api/admin/payments/{'a' * 32}/refund"
    assert client.post(path, headers=headers, json={}).status_code == 502
    assert client.post(path, headers=headers, json={}).status_code == 409
    with sessions() as db:
        assert db.get(Payment, payment_id).status == "refund_pending"
        assert db.scalar(select(PaymentTransaction).where(PaymentTransaction.event_type == "refund.requested")).status == "submitted"


def test_email_retries_and_worker_expiration(client, sessions, room_id, monkeypatch):
    from backend.app import outbox
    from backend.app.worker import maintenance
    reserve(client, room_id)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from_email", "resort@example.com")
    monkeypatch.setattr(outbox, "send_email", lambda *a, **kw: False)
    assert outbox.deliver_pending(session_factory=sessions) == 0
    with sessions() as db:
        message = db.scalar(select(EmailOutbox))
        assert message.attempts == 1
        assert message.status == "pending"
        message.available_at = datetime.utcnow() - timedelta(seconds=1)
        db.scalar(select(Booking)).expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
    monkeypatch.setattr(outbox, "send_email", lambda *a, **kw: True)
    assert maintenance(session_factory=sessions) == {"expired_holds": 1, "emails_sent": 1}
    assert outbox.deliver_pending(session_factory=sessions) == 0


def test_request_security_and_body_limit(client):
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert len(response.headers["x-request-id"]) == 32
    assert client.post("/api/contact", content=b"x" * 65537).status_code == 413
    assert client.get("/.env").status_code == 404
    assert client.get("/backend/app/config.py").status_code == 404
    assert client.post("/api/internal/maintenance").status_code == 404


def test_production_config_requires_postgres_and_https():
    with pytest.raises(ValueError):
        Settings(_env_file=None, environment="production")
    value = Settings(_env_file=None, environment="production", database_url="postgres://user:password@localhost/db",
        jwt_secret="x" * 48, cors_origins=["https://resort.example.com"], allowed_hosts=["resort.example.com"])
    assert value.database_url.startswith("postgresql+psycopg://")


def test_concurrent_webhooks_are_idempotent(client, sessions, room_id, monkeypatch):
    create_payment(sessions, room_id)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-webhook-secret")
    body = json.dumps(capture_event()).encode()
    headers = {"X-Razorpay-Signature": hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest(),
               "X-Razorpay-Event-Id": "evt_concurrent"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post("/api/payments/webhook", content=body, headers=headers), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    with sessions() as db:
        assert db.scalar(select(func.count(WebhookEvent.event_id))) == 1
        assert db.scalar(select(func.count(EmailOutbox.id))) == 1


def test_review_requires_completed_stay_and_moderation(client, sessions, room_id):
    from backend.app.models import Review
    booking = reserve(client, room_id)
    payload = {"booking_ref": booking["booking_ref"], "email": "guest@example.com", "rating": 5, "comment": "Lovely stay"}
    assert client.post("/api/reviews", json=payload).status_code == 404
    with sessions() as db:
        row = db.scalar(select(Booking))
        row.status = "confirmed"
        row.payment_status = "paid"
        db.commit()
    assert client.post("/api/reviews", json=payload).status_code == 409
    with sessions() as db:
        row = db.scalar(select(Booking))
        row.check_in = date.today() - timedelta(days=3)
        row.check_out = date.today() - timedelta(days=1)
        db.commit()
    response = client.post("/api/reviews", json=payload)
    assert response.status_code == 201
    assert client.get("/api/reviews").json() == []
    assert client.put(f"/api/admin/reviews/{response.json()['id']}/publish?is_published=true", headers=login(client)).status_code == 200
    assert client.get("/api/reviews").json()[0]["comment"] == payload["comment"]
    assert client.post("/api/reviews", json=payload).status_code == 409
    with sessions() as db:
        assert db.scalar(select(func.count(Review.id))) == 1


def test_admin_pagination(client, sessions, room_id):
    from backend.app.models import ContactEnquiry
    with sessions() as db:
        db.add_all([ContactEnquiry(name=f"Guest {i}", email="guest@example.com", message="Please share details.") for i in range(4)])
        db.commit()
    headers = login(client)
    first = client.get("/api/admin/enquiries?limit=2&offset=0", headers=headers).json()
    second = client.get("/api/admin/enquiries?limit=2&offset=2", headers=headers).json()
    assert len(first) == len(second) == 2
    assert not ({row["id"] for row in first} & {row["id"] for row in second})


def test_failed_booking_does_not_enqueue_email(client, sessions, room_id):
    reserve(client, room_id)
    start, end = stay_dates()
    assert client.post("/api/bookings", json=booking_payload(room_id, start, end)).status_code == 409
    with sessions() as db:
        assert db.scalar(select(func.count(EmailOutbox.id))) == 1


def test_reclaimed_email_lease_can_be_delivered(sessions, monkeypatch):
    from backend.app import outbox
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from_email", "resort@example.com")
    monkeypatch.setattr(outbox, "send_email", lambda *args, **kw: True)
    with sessions() as db:
        db.add(EmailOutbox(recipient="guest@example.com", subject="Stay", body_html="<p>Stay</p>", body_text="Stay",
            status="sending", attempts=1, lease_token="dead-worker", available_at=datetime.utcnow() - timedelta(seconds=1)))
        db.commit()
    assert outbox.deliver_pending(session_factory=sessions) == 1
    with sessions() as db:
        row = db.scalar(select(EmailOutbox))
        assert row.status == "sent" and row.attempts == 2 and row.lease_token is None


def test_maintenance_requires_secret(client, monkeypatch):
    import backend.app.main as main
    monkeypatch.setattr(settings, "maintenance_secret", "maintenance-secret-for-testing")
    monkeypatch.setattr(main, "maintenance", lambda **kwargs: {"expired_holds": 0, "emails_sent": 0})
    assert client.post("/api/internal/maintenance").status_code == 401
    assert client.post("/api/internal/maintenance", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/internal/maintenance", headers={"Authorization": "Bearer maintenance-secret-for-testing"}).status_code == 200


def test_rate_limit_blocks_repeated_invalid_login(client, monkeypatch):
    from backend.app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", True)
    limiter.reset()
    responses = [client.post("/api/auth/login", json={"email": "missing@example.com", "password": "incorrect"}) for _ in range(11)]
    assert responses[-1].status_code == 429
    limiter.reset()


def test_http_email_provider_uses_stable_retry_key(monkeypatch):
    import httpx
    from backend.app.email_service import send_email
    monkeypatch.setattr(settings, "resend_api_key", "test-key")
    monkeypatch.setattr(settings, "email_from", "desk@example.com")
    requests = []
    def send(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json={"id": "email_123"}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", send)
    assert send_email("guest@example.com", "Booking", "<p>Stay</p>", "Stay", idempotency_key="stable-email-key")
    assert requests[0][0] == "https://api.resend.com/emails"
    assert requests[0][1]["headers"]["Idempotency-Key"] == "stable-email-key"

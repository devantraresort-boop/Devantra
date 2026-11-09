"""Transactional email queue. SMTP is at-least-once; a crash after send can redeliver."""
import uuid
import hashlib
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.email_service import email_configured, send_email
from backend.app.models import EmailOutbox


def enqueue_email(db, recipient, subject, body_html, body_text, *, dedupe_key=None):
    if dedupe_key and db.scalar(select(EmailOutbox.id).where(EmailOutbox.dedupe_key == dedupe_key)):
        return
    db.add(EmailOutbox(recipient=recipient, subject=subject, body_html=body_html,
                       body_text=body_text, dedupe_key=dedupe_key or uuid.uuid4().hex))


def deliver_pending(*, limit=20, session_factory=SessionLocal) -> int:
    if not email_configured():
        return 0  # Retain messages until delivery is configured.
    sent = 0
    for _ in range(limit):
        now = datetime.utcnow()
        with session_factory() as db:
            row = db.scalar(select(EmailOutbox).where(
                EmailOutbox.status.in_(("pending", "sending")),
                EmailOutbox.available_at <= now,
            ).order_by(EmailOutbox.id).limit(1).with_for_update(skip_locked=True))
            if row is None:
                break
            if row.attempts >= settings.email_max_attempts:
                row.status = "failed"
                db.commit()
                continue
            row.status = "sending"
            row.attempts += 1
            row.lease_token = token = uuid.uuid4().hex
            row.available_at = now + timedelta(minutes=10)
            row_id = row.id
            message = (row.recipient, row.subject, row.body_html, row.body_text)
            delivery_key = hashlib.sha256((row.dedupe_key or str(row.id)).encode()).hexdigest()
            db.commit()
        # Network I/O never holds a database connection/transaction open.
        success = send_email(*message, idempotency_key=delivery_key)
        with session_factory() as db:
            row = db.scalar(select(EmailOutbox).where(
                EmailOutbox.id == row_id, EmailOutbox.lease_token == token,
            ).with_for_update())
            if row is None:
                continue
            row.lease_token = None
            if success:
                row.status = "sent"
                row.sent_at = datetime.utcnow()
                sent += 1
            else:
                row.status = "failed" if row.attempts >= settings.email_max_attempts else "pending"
                row.available_at = datetime.utcnow() + timedelta(seconds=min(3600, 30 * 2 ** row.attempts))
            if row.status == "failed":
                logging.getLogger(__name__).error("Email job %s exhausted retries", row.id)
            db.commit()
    return sent

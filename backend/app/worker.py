"""Run periodically with --once, or continuously as a separate worker process."""
import argparse
import logging
import signal
import threading

from sqlalchemy import select

from backend.app.booking_service import expire_booking
from backend.app.database import SessionLocal
from backend.app.models import Booking
from backend.app.outbox import deliver_pending

logger = logging.getLogger(__name__)


def maintenance(*, session_factory=SessionLocal, email_limit=20):
    from datetime import datetime
    with session_factory() as db:
        rows = db.scalars(select(Booking).where(
            Booking.status == "pending_payment", Booking.expires_at <= datetime.utcnow(),
        ).order_by(Booking.id).limit(500).with_for_update(skip_locked=True)).all()
        expired = sum(expire_booking(row) for row in rows)
        db.commit()
    sent = deliver_pending(limit=email_limit, session_factory=session_factory)
    return {"expired_holds": expired, "emails_sent": sent}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    while not stopping.is_set():
        try:
            result = maintenance()
            logger.info("Maintenance: %s", result)
        except Exception:
            logger.error("Maintenance failed; check database and email connectivity.")
            if args.once:
                raise
        if args.once:
            break
        stopping.wait(10)


if __name__ == "__main__":
    main()

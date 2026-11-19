from backend.app.room_photos import set_room_photos
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from backend.app.database import get_db
from backend.app.email_service import booking_status_email, email_configured
from backend.app.outbox import enqueue_email
from backend.app.booking_service import active_bookings, occupancy_peak, resort_today
from backend.app.routers.bookings import _out as booking_out
from backend.app.config import settings
from backend.app.models import (
    Admin,
    AdminActivityLog,
    Amenity,
    Booking,
    ContactEnquiry,
    GalleryImage,
    Package,
    Payment,
    Room,
    RoomAvailability,
    RoomCategory,
    Review,
    SiteSettings,
    EmailOutbox,
)
from backend.app.schemas import (
    AmenityUpdate,
    AvailabilityUpdate,
    BookingCancellation,
    ContentCreate,
    CreateCategory,
    EnquiryStatusUpdate,
    GalleryCreate,
    PackageCreate,
    PackageUpdate,
    RoomCreate,
    RoomUpdate,
    GalleryUpdate,
    SiteSettingsUpdate,
)
from backend.app.security import require_admin
from backend.app.routers.rooms import _room_out


router = APIRouter(prefix="/api/admin", tags=["admin"])


def _activity(db: Session, admin: Admin, action: str, entity_type: str, entity_id: str, details: str = ""):
    db.add(
        AdminActivityLog(
            admin_id=admin.id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
        )
    )


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), _admin: Admin = Depends(require_admin)):
    today = resort_today()
    from zoneinfo import ZoneInfo
    from datetime import timezone
    start = datetime.combine(today, time.min, tzinfo=ZoneInfo(settings.resort_timezone)).astimezone(timezone.utc).replace(tzinfo=None)
    end = start + timedelta(days=1)
    total_bookings = db.scalar(select(func.count(Booking.id))) or 0
    today_bookings = db.scalar(select(func.count(Booking.id)).where(Booking.created_at >= start, Booking.created_at < end)) or 0
    pending = db.scalar(
        select(func.count(Booking.id)).where(
            Booking.status == "pending_payment",
            Booking.expires_at > datetime.utcnow(),
        )
    ) or 0
    confirmed = db.scalar(select(func.count(Booking.id)).where(Booking.status == "confirmed")) or 0
    cancelled = db.scalar(
        select(func.count(Booking.id)).where(
            (Booking.status == "cancelled")
            | (Booking.status == "expired")
            | ((Booking.status == "pending_payment") & (Booking.expires_at <= datetime.utcnow()))
        )
    ) or 0
    revenue = db.scalar(
        select(func.coalesce(func.sum(Payment.amount - Payment.refund_amount), 0)).where(
            Payment.status.in_(("captured", "partially_refunded", "refunded", "refund_pending"))
        )
    ) or Decimal("0.00")
    enquiries = db.scalar(select(func.count(ContactEnquiry.id)).where(ContactEnquiry.status == "new")) or 0
    return {
        "total_bookings": total_bookings,
        "today_bookings": today_bookings,
        "pending_bookings": pending,
        "confirmed_bookings": confirmed,
        "cancelled_bookings": cancelled,
        "revenue": revenue,
        "new_enquiries": enquiries,
    }


@router.get("/categories")
def categories(db: Session = Depends(get_db), _admin: Admin = Depends(require_admin)):
    rows = db.scalars(select(RoomCategory).order_by(RoomCategory.name)).all()
    return [{"id": row.id, "name": row.name, "description": row.description} for row in rows]


@router.post("/categories", status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CreateCategory,
    db: Session = Depends(get_db),
    admin: Admin = Depends(require_admin),
):
    if db.scalar(select(RoomCategory.id).where(RoomCategory.name == payload.name)):
        raise HTTPException(status_code=409, detail="A room category with this name already exists.")
    category = RoomCategory(name=payload.name, description=payload.description)
    db.add(category)
    db.flush()
    _activity(db, admin, "category.create", "room_category", str(category.id))
    db.commit()
    return {"id": category.id, "name": category.name, "description": category.description}


@router.get("/rooms")
def admin_rooms(db: Session = Depends(get_db), _admin: Admin = Depends(require_admin)):
    rooms = db.scalars(select(Room).options(joinedload(Room.category)).order_by(Room.id)).all()
    return [{**_room_out(room), "is_active": room.is_active} for room in rooms]


@router.post("/rooms", status_code=status.HTTP_201_CREATED)
def create_room(payload: RoomCreate, db: Session = Depends(get_db), admin: Admin = Depends(require_admin)):
    category = db.get(RoomCategory, payload.category_id)
    if category is None:
        raise HTTPException(status_code=422, detail="Room category not found.")
    values = payload.model_dump(exclude={"photos"})
    room = Room(**values)
    set_room_photos(room, payload.photos if payload.photos is not None else ([payload.image_url] if payload.image_url else []))
    db.add(room)
    db.flush()
    _activity(db, admin, "room.create", "room", str(room.id))
    db.commit()
    db.refresh(room)
    return {**_room_out(room), "is_active": room.is_active}


@router.put("/rooms/{room_id}")
def update_room(
    room_id: int,
    payload: RoomUpdate,
    db: Session = Depends(get_db),
    admin: Admin = Depends(require_admin),
):
    room = db.scalar(select(Room).where(Room.id == room_id).with_for_update(of=Room))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found.")
    changes = payload.model_dump(exclude_unset=True)
    stays = db.execute(select(Booking.check_in, Booking.check_out, Booking.adults, Booking.children).where(
        Booking.room_id == room_id, Booking.check_out > resort_today(), active_bookings(datetime.utcnow()),
    )).all()
    if stays:
        last = max(row.check_out for row in stays)
        peak = occupancy_peak([(row.check_in, row.check_out) for row in stays], resort_today(), last)
        if changes.get("inventory", room.inventory) < peak:
            raise HTTPException(status_code=409, detail="Inventory cannot be reduced below existing reservations.")
        if any(row.adults > changes.get("capacity_adults", room.capacity_adults) or
               row.children > changes.get("capacity_children", room.capacity_children) for row in stays):
            raise HTTPException(status_code=409, detail="Capacity cannot be reduced below existing reservations.")
    if "category_id" in changes and db.get(RoomCategory, changes["category_id"]) is None:
        raise HTTPException(status_code=422, detail="Room category not found.")
    photos = changes.pop("photos", None)
    if photos is not None:
        set_room_photos(room, photos)
        changes.pop("image_url", None)
    elif "image_url" in changes and changes["image_url"] != room.image_url:
        set_room_photos(room, [changes["image_url"]] if changes["image_url"] else [])
    for field, value in changes.items():
        setattr(room, field, value)
    _activity(db, admin, "room.update", "room", str(room.id))
    db.commit()
    db.refresh(room)
    return {**_room_out(room), "is_active": room.is_active}


@router.delete("/rooms/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_room(room_id: int, db: Session = Depends(get_db), admin: Admin = Depends(require_admin)):
    room = db.scalar(select(Room).where(Room.id == room_id).with_for_update(of=Room))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found.")
    # Booking creation takes this same room lock. Keep every historical booking
    # and its room reference intact, but allow unused room entries to be removed.
    has_bookings = db.scalar(select(Booking.id).where(Booking.room_id == room_id).limit(1)) is not None
    if has_bookings:
        room.is_active = False
        _activity(db, admin, "room.archive", "room", str(room.id))
    else:
        db.delete(room)
        _activity(db, admin, "room.delete", "room", str(room.id))
    db.commit()


@router.put("/availability")
def update_availability(
    payload: AvailabilityUpdate,
    db: Session = Depends(get_db),
    admin: Admin = Depends(require_admin),
):
    room = db.scalar(select(Room).where(Room.id == payload.room_id).with_for_update(of=Room))
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found.")
    day_count = (payload.end_date - payload.start_date).days + 1
    dates = [payload.start_date + timedelta(days=offset) for offset in range(day_count)]
    if payload.is_closed:
        now = datetime.utcnow()
        conflict = db.scalar(
            select(Booking.id)
            .where(
                Booking.room_id == room.id,
                Booking.status.in_(("pending_payment", "confirmed")),
                Booking.check_in <= payload.end_date,
                Booking.check_out > payload.start_date,
                (
                    (Booking.status == "confirmed")
                    | ((Booking.expires_at.is_not(None)) & (Booking.expires_at > now))
                ),
            )
            .limit(1)
        )
        if conflict is not None:
            raise HTTPException(status_code=409, detail="Cannot close dates that overlap an active booking.")

    existing = {row.stay_date: row for row in db.scalars(select(RoomAvailability).where(
        RoomAvailability.room_id == room.id, RoomAvailability.stay_date >= payload.start_date,
        RoomAvailability.stay_date <= payload.end_date))}
    for stay_date in dates:
        record = existing.get(stay_date)
        if record is None:
            record = RoomAvailability(room_id=room.id, stay_date=stay_date)
            db.add(record)
        record.is_closed = payload.is_closed
        record.price_override = payload.price_override
        record.updated_by_admin_id = admin.id
    _activity(
        db,
        admin,
        "availability.update",
        "room",
        str(room.id),
        f"{payload.start_date}–{payload.end_date}",
    )
    db.commit()
    return {"room_id": room.id, "updated_dates": day_count}


@router.get("/bookings")
def admin_bookings(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: Admin = Depends(require_admin),
):
    query = select(Booking).options(joinedload(Booking.room)).order_by(Booking.created_at.desc(), Booking.id.desc()).offset(offset).limit(limit)
    if status_filter:
        if status_filter not in {"pending_payment", "confirmed", "cancelled", "expired"}:
            raise HTTPException(status_code=422, detail="Unknown booking status.")
        if status_filter == "expired":
            query = query.where((Booking.status == "expired") | ((Booking.status == "pending_payment") & (Booking.expires_at <= datetime.utcnow())))
        elif status_filter == "pending_payment":
            query = query.where(Booking.status == status_filter, Booking.expires_at > datetime.utcnow())
        else:
            query = query.where(Booking.status == status_filter)
    rows = db.scalars(query).unique().all()
    return [
        {
            "booking_ref": row.booking_ref,
            "room_name": row.room.name,
            "guest_name": row.guest_name,
            "guest_email": row.guest_email,
            "guest_phone": row.guest_phone,
            "check_in": row.check_in,
            "check_out": row.check_out,
            "adults": row.adults,
            "children": row.children,
            "total_amount": row.total_amount,
            "status": booking_out(row)["status"],
            "payment_status": row.payment_status,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post("/bookings/{booking_ref}/cancel")
def admin_cancel_booking(
    booking_ref: str,
    payload: BookingCancellation,
    db: Session = Depends(get_db),
    admin: Admin = Depends(require_admin),
):
    booking = db.scalar(
        select(Booking).options(joinedload(Booking.room)).where(Booking.booking_ref == booking_ref).with_for_update(of=Booking)
    )
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found.")
    if booking.status == "cancelled":
        return {"booking_ref": booking.booking_ref, "status": booking.status}
    booking.status = "cancelled"
    booking.cancellation_reason = payload.reason or "Cancelled by resort administrator."
    booking.updated_at = datetime.utcnow()
    _activity(db, admin, "booking.cancel", "booking", booking.booking_ref, booking.cancellation_reason)
    html_body, text_body = booking_status_email(
        booking.guest_name,
        booking.booking_ref,
        "Your booking was cancelled by the resort",
        booking.cancellation_reason,
    )
    enqueue_email(
        db,
        booking.guest_email,
        f"Booking cancelled · {settings.resort_name}",
        html_body,
        text_body,
    )
    db.commit()
    return {"booking_ref": booking.booking_ref, "status": booking.status, "payment_status": booking.payment_status}


@router.get("/enquiries")
def admin_enquiries(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: Admin = Depends(require_admin),
):
    query = select(ContactEnquiry).order_by(ContactEnquiry.created_at.desc(), ContactEnquiry.id.desc()).offset(offset).limit(limit)
    if status_filter:
        query = query.where(ContactEnquiry.status == status_filter)
    return db.scalars(query).all()


@router.put("/enquiries/{enquiry_id}")
def update_enquiry(
    enquiry_id: int,
    payload: EnquiryStatusUpdate,
    db: Session = Depends(get_db),
    admin: Admin = Depends(require_admin),
):
    enquiry = db.get(ContactEnquiry, enquiry_id)
    if enquiry is None:
        raise HTTPException(status_code=404, detail="Enquiry not found.")
    enquiry.status = payload.status
    _activity(db, admin, "enquiry.update", "enquiry", str(enquiry_id), payload.status)
    db.commit()
    return {"id": enquiry.id, "status": enquiry.status}


@router.get("/amenities")
def admin_amenities(db: Session = Depends(get_db), _admin: Admin = Depends(require_admin)):
    return db.scalars(select(Amenity).order_by(Amenity.id)).all()


@router.post("/amenities", status_code=status.HTTP_201_CREATED)
def create_amenity(payload: ContentCreate, db: Session = Depends(get_db), admin: Admin = Depends(require_admin)):
    if db.scalar(select(Amenity.id).where(Amenity.name == payload.name)):
        raise HTTPException(status_code=409, detail="Amenity already exists.")
    amenity = Amenity(name=payload.name, description=payload.description, icon=payload.icon, is_active=payload.is_active)
    db.add(amenity)
    db.flush()
    _activity(db, admin, "amenity.create", "amenity", str(amenity.id))
    db.commit()
    return amenity


@router.put("/amenities/{amenity_id}")
def update_amenity(
    amenity_id: int,
    payload: AmenityUpdate,
    db: Session = Depends(get_db),
    admin: Admin = Depends(require_admin),
):
    amenity = db.get(Amenity, amenity_id)
    if amenity is None:
        raise HTTPException(status_code=404, detail="Amenity not found.")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(amenity, field, value)
    _activity(db, admin, "amenity.update", "amenity", str(amenity_id))
    db.commit()
    return amenity


@router.delete("/amenities/{amenity_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_amenity(amenity_id: int, db: Session = Depends(get_db), admin: Admin = Depends(require_admin)):
    amenity = db.get(Amenity, amenity_id)
    if amenity is None:
        raise HTTPException(status_code=404, detail="Amenity not found.")
    amenity.is_active = False
    _activity(db, admin, "amenity.archive", "amenity", str(amenity_id))
    db.commit()


@router.get("/packages")
def admin_packages(db: Session = Depends(get_db), _admin: Admin = Depends(require_admin)):
    return db.scalars(select(Package).order_by(Package.id)).all()


@router.post("/packages", status_code=status.HTTP_201_CREATED)
def create_package(payload: PackageCreate, db: Session = Depends(get_db), admin: Admin = Depends(require_admin)):
    if db.scalar(select(Package.id).where(Package.name == payload.name)):
        raise HTTPException(status_code=409, detail="Package already exists.")
    package = Package(**payload.model_dump())
    db.add(package)
    db.flush()
    _activity(db, admin, "package.create", "package", str(package.id))
    db.commit()
    return package


@router.put("/packages/{package_id}")
def update_package(
    package_id: int,
    payload: PackageUpdate,
    db: Session = Depends(get_db),
    admin: Admin = Depends(require_admin),
):
    package = db.get(Package, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    changes = payload.model_dump(exclude_unset=True)
    starts_on = changes.get("starts_on", package.starts_on)
    ends_on = changes.get("ends_on", package.ends_on)
    if starts_on and ends_on and ends_on < starts_on:
        raise HTTPException(status_code=422, detail="Package end date must be on or after its start date.")
    for field, value in changes.items():
        setattr(package, field, value)
    _activity(db, admin, "package.update", "package", str(package_id))
    db.commit()
    return package


@router.delete("/packages/{package_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_package(package_id: int, db: Session = Depends(get_db), admin: Admin = Depends(require_admin)):
    package = db.get(Package, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    package.is_active = False
    _activity(db, admin, "package.archive", "package", str(package_id))
    db.commit()


@router.get("/gallery")
def admin_gallery(db: Session = Depends(get_db), _admin: Admin = Depends(require_admin)):
    return db.scalars(select(GalleryImage).order_by(GalleryImage.sort_order, GalleryImage.id)).all()


@router.post("/gallery", status_code=status.HTTP_201_CREATED)
def create_gallery_image(
    payload: GalleryCreate,
    db: Session = Depends(get_db),
    admin: Admin = Depends(require_admin),
):
    image = GalleryImage(**payload.model_dump())
    db.add(image)
    db.flush()
    _activity(db, admin, "gallery.create", "gallery_image", str(image.id))
    db.commit()
    return image


@router.put("/gallery/{image_id}")
def update_gallery_image(
    image_id: int,
    payload: GalleryUpdate,
    db: Session = Depends(get_db),
    admin: Admin = Depends(require_admin),
):
    image = db.get(GalleryImage, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="Gallery image not found.")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(image, field, value)
    _activity(db, admin, "gallery.update", "gallery_image", str(image_id))
    db.commit()
    return image


@router.delete("/gallery/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
def unpublish_gallery_image(image_id: int, db: Session = Depends(get_db), admin: Admin = Depends(require_admin)):
    image = db.get(GalleryImage, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="Gallery image not found.")
    image.is_published = False
    _activity(db, admin, "gallery.unpublish", "gallery_image", str(image_id))
    db.commit()


@router.get("/reviews")
def admin_reviews(limit: int = Query(default=100, ge=1, le=250), offset: int = Query(default=0, ge=0),
                  db: Session = Depends(get_db), _admin: Admin = Depends(require_admin)):
    return db.scalars(select(Review).order_by(Review.created_at.desc(), Review.id.desc()).offset(offset).limit(limit)).all()


@router.delete("/reviews/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_review(review_id: int, db: Session = Depends(get_db), admin: Admin = Depends(require_admin)):
    review = db.get(Review, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    db.delete(review)
    _activity(db, admin, "review.delete", "review", str(review_id))
    db.commit()


@router.put("/reviews/{review_id}/publish")
def publish_review(
    review_id: int,
    is_published: bool = Query(...),
    db: Session = Depends(get_db),
    admin: Admin = Depends(require_admin),
):
    review = db.get(Review, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    review.is_published = is_published
    _activity(db, admin, "review.moderate", "review", str(review_id), str(is_published))
    db.commit()
    return {"id": review.id, "is_published": review.is_published}


@router.get("/activity")
def admin_activity(
    limit: int = Query(default=100, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: Admin = Depends(require_admin),
):
    return db.scalars(select(AdminActivityLog).order_by(AdminActivityLog.created_at.desc(), AdminActivityLog.id.desc()).offset(offset).limit(limit)).all()


@router.put("/site-info")
def update_site_info(payload: SiteSettingsUpdate, db: Session = Depends(get_db), admin: Admin = Depends(require_admin)):
    record = db.scalar(select(SiteSettings).where(SiteSettings.id == 1).with_for_update())
    if record is None:
        record = SiteSettings(id=1, **payload.model_dump())
        db.add(record)
    else:
        for field, value in payload.model_dump().items():
            setattr(record, field, value)
    _activity(db, admin, "site.update", "site_settings", "1")
    db.commit()
    return payload.model_dump()


@router.get("/availability")
def availability_calendar(room_id: int, start_date: date, end_date: date,
                          db: Session = Depends(get_db), _admin: Admin = Depends(require_admin)):
    if end_date < start_date or (end_date - start_date).days > 730:
        raise HTTPException(status_code=422, detail="Choose a date range of at most two years.")
    return db.scalars(select(RoomAvailability).where(RoomAvailability.room_id == room_id,
        RoomAvailability.stay_date >= start_date, RoomAvailability.stay_date <= end_date)
        .order_by(RoomAvailability.stay_date)).all()


@router.get("/notifications")
def notification_status(db: Session = Depends(get_db), _admin: Admin = Depends(require_admin)):
    counts = dict(db.execute(select(EmailOutbox.status, func.count()).group_by(EmailOutbox.status)).all())
    return {"email_configured": email_configured(), "counts": counts}


@router.post("/notifications/retry")
def retry_notifications(db: Session = Depends(get_db), admin: Admin = Depends(require_admin)):
    from sqlalchemy import update
    result = db.execute(update(EmailOutbox).where(EmailOutbox.status == "failed")
        .values(status="pending", attempts=0, available_at=datetime.utcnow(), lease_token=None))
    _activity(db, admin, "notifications.retry", "email_outbox", "", f"{result.rowcount} messages queued")
    db.commit()
    return {"queued": result.rowcount}

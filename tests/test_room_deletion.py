from datetime import date, timedelta

import pytest
from sqlalchemy import select

from backend.app.models import AdminActivityLog, Booking, Room, RoomAvailability, RoomPhoto
from tests.test_booking_api import booking_payload, stay_dates
from tests.test_room_photos import auth, photo


def test_delete_unused_room_removes_photos_and_availability(client, sessions, room_id):
    headers = auth(client)
    response = client.put(f"/api/admin/rooms/{room_id}", headers=headers, json={"photos": [photo("blue")]})
    photo_url = response.json()["photos"][0]
    with sessions() as db:
        db.add(RoomAvailability(room_id=room_id, stay_date=date.today() + timedelta(days=2)))
        db.commit()
    assert client.delete(f"/api/admin/rooms/{room_id}").status_code == 401
    assert client.get(f"/api/rooms/{room_id}").status_code == 200
    assert client.delete(f"/api/admin/rooms/{room_id}", headers=headers).status_code == 204
    assert client.get(f"/api/rooms/{room_id}").status_code == 404
    assert client.get(photo_url).status_code == 404
    assert all(room["id"] != room_id for room in client.get("/api/admin/rooms", headers=headers).json())
    with sessions() as db:
        assert db.get(Room, room_id) is None
        assert db.scalar(select(RoomPhoto.id).where(RoomPhoto.room_id == room_id)) is None
        assert db.scalar(select(RoomAvailability.id).where(RoomAvailability.room_id == room_id)) is None
        assert db.scalar(select(AdminActivityLog.id).where(AdminActivityLog.action == "room.delete")) is not None
    assert client.delete(f"/api/admin/rooms/{room_id}", headers=headers).status_code == 404


@pytest.mark.parametrize("booking_status", ["pending_payment", "confirmed", "cancelled"])
def test_delete_room_with_any_booking_history_archives_and_preserves_lookup(client, sessions, room_id, booking_status):
    headers = auth(client)
    start, end = stay_dates()
    response = client.post("/api/bookings", json=booking_payload(room_id, start, end))
    assert response.status_code == 201
    reference = response.json()["booking_ref"]
    with sessions() as db:
        booking = db.scalar(select(Booking).where(Booking.booking_ref == reference))
        booking.status = booking_status
        db.commit()
    assert client.delete(f"/api/admin/rooms/{room_id}", headers=headers).status_code == 204
    assert client.get(f"/api/rooms/{room_id}").status_code == 404
    rooms = client.get("/api/admin/rooms", headers=headers).json()
    assert next(room for room in rooms if room["id"] == room_id)["is_active"] is False
    lookup = client.post("/api/bookings/lookup", json={"booking_ref": reference, "email": "guest@example.com"})
    assert lookup.status_code == 200
    assert lookup.json()["room_name"] == "Test Room"
    assert lookup.json()["status"] == booking_status
    assert client.post("/api/bookings", json=booking_payload(room_id, start, end)).status_code == 404
    with sessions() as db:
        assert db.get(Room, room_id) is not None
        assert db.scalar(select(Booking.id).where(Booking.booking_ref == reference)) is not None
        assert db.scalar(select(AdminActivityLog.id).where(AdminActivityLog.action == "room.archive")) is not None

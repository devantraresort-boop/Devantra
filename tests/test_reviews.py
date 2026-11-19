from datetime import timedelta

from sqlalchemy import select

from backend.app.models import Booking, Review, AdminActivityLog
from backend.app.booking_service import resort_today
from tests.test_booking_api import booking_payload, stay_dates
from tests.test_room_photos import auth


def test_review_submission_moderation_and_deletion(client, sessions, room_id):
    start, end = stay_dates()
    booking = client.post('/api/bookings', json=booking_payload(room_id, start, end)).json()
    payload = dict(booking_ref=booking['booking_ref'], email='guest@example.com', rating=5, comment='A lovely stay.')
    assert client.post('/api/reviews', json=payload).status_code == 404
    with sessions() as db:
        row = db.scalar(select(Booking).where(Booking.booking_ref == booking['booking_ref']))
        row.status = 'confirmed'
        db.commit()
    assert client.post('/api/reviews', json=payload).status_code == 409
    with sessions() as db:
        row = db.scalar(select(Booking).where(Booking.booking_ref == booking['booking_ref']))
        row.check_in = resort_today() - timedelta(days=3)
        row.check_out = resort_today() - timedelta(days=1)
        db.commit()
    assert client.post('/api/reviews', json={**payload, 'rating': 6}).status_code == 422
    assert client.post('/api/reviews', json={**payload, 'rating': 0}).status_code == 422
    response = client.post('/api/reviews', json=payload)
    assert response.status_code == 201
    review_id = response.json()['id']
    assert client.get('/api/reviews').json()[0]['rating'] == 5
    assert client.post('/api/reviews', json=payload).status_code == 409
    headers = auth(client)
    assert client.get('/api/admin/reviews', headers=headers).json()[0]['rating'] == 5
    assert client.delete(f'/api/admin/reviews/{review_id}').status_code == 401
    assert client.put(f'/api/admin/reviews/{review_id}/publish?is_published=true', headers=headers).status_code == 200
    assert client.get('/api/reviews').json()[0]['comment'] == payload['comment']
    assert client.delete(f'/api/admin/reviews/{review_id}', headers=headers).status_code == 204
    assert client.get('/api/reviews').json() == []
    assert client.get('/api/admin/reviews', headers=headers).json() == []
    assert client.delete(f'/api/admin/reviews/{review_id}', headers=headers).status_code == 404
    with sessions() as db:
        assert db.get(Review, review_id) is None
        assert db.scalar(select(AdminActivityLog.id).where(AdminActivityLog.action == 'review.delete')) is not None


def test_guest_review_without_booking(client, sessions):
    payload = {'guest_name': '  Walk-in Guest  ', 'rating': 4, 'comment': 'Lovely experience'}
    for invalid in [{'guest_name': '   '}, {'rating': 0}, {'rating': 6}]:
        assert client.post('/api/reviews', json={**payload, **invalid}).status_code == 422
    response = client.post('/api/reviews', json=payload)
    assert response.status_code == 201
    review_id = response.json()['id']
    assert client.get('/api/reviews').json()[0]['rating'] == 4
    headers = auth(client)
    rows = client.get('/api/admin/reviews', headers=headers).json()
    assert rows[0]['guest_name'] == 'Walk-in Guest'
    assert rows[0]['rating'] == 4
    with sessions() as db:
        assert db.get(Review, review_id).booking_id is None
    assert client.put(f'/api/admin/reviews/{review_id}/publish?is_published=true', headers=headers).status_code == 200
    assert client.get('/api/reviews').json()[0]['rating'] == 4
    assert client.delete(f'/api/admin/reviews/{review_id}', headers=headers).status_code == 204
    assert client.get('/api/reviews').json() == []

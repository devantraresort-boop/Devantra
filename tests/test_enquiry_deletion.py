from sqlalchemy import select
from backend.app.models import ContactEnquiry, AdminActivityLog
from tests.test_room_photos import auth


def test_admin_delete_enquiry(client, sessions):
    with sessions() as db:
        enquiry = ContactEnquiry(name='Guest', email='guest@example.com', message='Please send room details')
        db.add(enquiry)
        db.commit()
        enquiry_id = enquiry.id
    url = f'/api/admin/enquiries/{enquiry_id}'
    assert client.delete(url).status_code == 401
    headers = auth(client)
    assert client.delete(url, headers=headers).status_code == 204
    assert client.get('/api/admin/enquiries', headers=headers).json() == []
    assert client.delete(url, headers=headers).status_code == 404
    with sessions() as db:
        assert db.get(ContactEnquiry, enquiry_id) is None
        assert db.scalar(select(AdminActivityLog.id).where(AdminActivityLog.action == 'enquiry.delete')) is not None

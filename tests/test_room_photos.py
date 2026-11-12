import base64
from io import BytesIO

from PIL import Image
from sqlalchemy import select

from backend.app.models import RoomPhoto


def photo(color):
    output = BytesIO()
    Image.new("RGB", (32, 24), color).save(output, "PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def auth(client):
    token = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "test-password-123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_gallery_save_cover_reorder_remove_and_public_bytes(client, room_id, sessions):
    headers = auth(client)
    sources = [photo(color) for color in ["red", "green", "blue", "white", "black"]]
    result = client.put(f"/api/admin/rooms/{room_id}", headers=headers, json={"photos": sources})
    assert result.status_code == 200, result.text
    urls = result.json()["photos"]
    assert len(urls) == 5 and result.json()["image_url"] == urls[0]
    assert client.get(f"/api/rooms/{room_id}").json()["photos"] == urls
    image = client.get(urls[0])
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    assert "public" in image.headers["cache-control"] and "no-store" not in image.headers["cache-control"]
    assert Image.open(BytesIO(image.content)).size == (32, 24)
    result = client.put(f"/api/admin/rooms/{room_id}", headers=headers, json={"photos": [urls[3], urls[1]]})
    assert result.json()["image_url"] == urls[3]
    assert result.json()["photos"] == [urls[3], urls[1]]
    assert client.get(urls[0]).status_code == 404
    with sessions() as db:
        assert len(db.scalars(select(RoomPhoto)).all()) == 2
    result = client.put(f"/api/admin/rooms/{room_id}", headers=headers, json={"photos": []})
    assert result.json()["photos"] == [] and result.json()["image_url"] == ""


def test_gallery_validation_is_atomic_and_requires_admin(client, room_id):
    headers = auth(client)
    path = f"/api/admin/rooms/{room_id}"
    assert client.put(path, json={"photos": [photo("red")]}).status_code == 401
    for invalid in [[photo("red")] * 6, ["data:image/jpeg;base64,bm90LWFuLWltYWdl"],
                    ["javascript:alert(1)"], ["/api/room-photos/" + "a" * 32],
                    [photo("red"), photo("red")], ["data:image/svg+xml;base64,PHN2Zy8+"]]:
        result = client.put(path, headers=headers, json={"name": "Should not save", "photos": invalid})
        assert result.status_code == 422
        assert client.get(f"/api/rooms/{room_id}").json()["name"] == "Test Room"


def test_create_with_photo_and_legacy_cover_preservation(client, room_id):
    headers = auth(client)
    room = client.get(f"/api/rooms/{room_id}").json()
    result = client.post("/api/admin/rooms", headers=headers, json={
        "category_id": room["category_id"], "name": "Photo suite", "base_rate": 4000,
        "photos": [photo("gold")],
    })
    assert result.status_code == 201, result.text
    assert len(result.json()["photos"]) == 1
    cover = "https://example.com/old-cover.jpg"
    path = f"/api/admin/rooms/{room_id}"
    assert client.put(path, headers=headers, json={"image_url": cover}).json()["photos"] == [cover]
    assert client.put(path, headers=headers, json={"name": "Keep cover"}).json()["photos"] == [cover]
    assert client.put(path, headers=headers, json={"photos": [cover, photo("blue")]}).json()["image_url"] == cover
    # An asset owned by another room cannot be adopted by reference.
    assert client.put(path, headers=headers, json={"photos": result.json()["photos"]}).status_code == 422


def test_full_size_photo_request_and_body_limit(client, room_id):
    headers = auth(client)
    output = BytesIO()
    Image.effect_noise((500, 500), 100).convert("RGB").save(output, "JPEG", quality=75)
    encoded = "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()
    assert len(encoded) > 65536
    assert client.put(f"/api/admin/rooms/{room_id}", headers=headers, json={"photos": [encoded]}).status_code == 200
    assert client.put(f"/api/admin/rooms/{room_id}", headers=headers, content=b" " * 3600001).status_code == 413

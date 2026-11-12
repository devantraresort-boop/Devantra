"""Bounded room galleries, saved atomically with their room."""
import base64
import binascii
from io import BytesIO
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError

from backend.app.models import Room, RoomPhoto


def decode_photo(value: str) -> bytes:
    try:
        header, encoded = value.split(",", 1)
        if header not in {"data:image/jpeg;base64", "data:image/png;base64", "data:image/webp;base64"}:
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 512000:
            raise ValueError()
        with Image.open(BytesIO(raw), formats=["JPEG", "PNG", "WEBP"]) as source:
            if source.width * source.height > 16000000:
                raise ValueError()
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1600, 1600))
            output = BytesIO()
            image.save(output, format="JPEG", quality=82, optimize=True)
            if output.tell() > 512000:
                output = BytesIO()
                image.thumbnail((1200, 1200))
                image.save(output, format="JPEG", quality=70, optimize=True)
            if output.tell() > 512000:
                raise ValueError()
            return output.getvalue()
    except (ValueError, binascii.Error, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise HTTPException(status_code=422, detail="Use a valid JPG, PNG or WebP photo, optimized to 500 KB or less.") from None


def set_room_photos(room: Room, values: list[str]) -> None:
    if len(values) > 5 or len(set(values)) != len(values):
        raise HTTPException(status_code=422, detail="Choose up to 5 different room photos.")
    existing = {photo.url: photo for photo in room.photos}
    photos = []
    for position, value in enumerate(values):
        if value in existing:
            photo = existing[value]
        elif value.startswith("data:"):
            photo = RoomPhoto(id=uuid4().hex, content=decode_photo(value), external_url="")
        else:
            parsed = urlsplit(value)
            if len(value) > 500 or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise HTTPException(status_code=422, detail="Use a photo uploaded for this room or an HTTPS image URL.")
            photo = RoomPhoto(id=uuid4().hex, external_url=value)
        photo.position = position
        photos.append(photo)
    room.photos = photos
    room.image_url = photos[0].url if photos else ""

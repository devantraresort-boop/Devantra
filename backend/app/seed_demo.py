"""Add clearly labelled sample rooms to a development/test database."""
from decimal import Decimal

from sqlalchemy import select

from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models import Room, RoomCategory


DEMO_ROOMS = (
    {
        "name": "Demo Deluxe Room", "base_rate": Decimal("3500.00"),
        "capacity_adults": 2, "capacity_children": 1, "inventory": 5,
        "amenities": "Wi-Fi, Air conditioning, King bed, Tea and coffee",
        "image_url": "https://images.unsplash.com/photo-1611892440504-42a792e24d32?auto=format&fit=crop&w=1000&q=80",
    },
    {
        "name": "Demo Garden Villa", "base_rate": Decimal("6500.00"),
        "capacity_adults": 2, "capacity_children": 2, "inventory": 3,
        "amenities": "Wi-Fi, Garden seating, King bed, Private balcony",
        "image_url": "https://images.unsplash.com/photo-1564013799919-ab600027ffc6?auto=format&fit=crop&w=1000&q=80",
    },
    {
        "name": "Demo Family Suite", "base_rate": Decimal("8500.00"),
        "capacity_adults": 4, "capacity_children": 2, "inventory": 2,
        "amenities": "Wi-Fi, Two bedrooms, Living area, Air conditioning",
        "image_url": "https://images.unsplash.com/photo-1566665797739-1674de7a421a?auto=format&fit=crop&w=1000&q=80",
    },
    {
        "name": "Demo Pool Villa", "base_rate": Decimal("12000.00"),
        "capacity_adults": 4, "capacity_children": 2, "inventory": 1,
        "amenities": "Wi-Fi, Private pool, Terrace, Two bedrooms",
        "image_url": "https://images.unsplash.com/photo-1571896349842-33c89424de2d?auto=format&fit=crop&w=1000&q=80",
    },
)


def seed_demo_rooms(db, *, environment: str) -> int:
    if environment not in {"development", "test"}:
        raise ValueError("Demo rooms can only be added in development or test environments.")
    category = db.scalar(select(RoomCategory).where(RoomCategory.name == "Demo stays (testing only)"))
    if category is None:
        category = RoomCategory(name="Demo stays (testing only)",
                                description="Sample inventory for testing; not real resort accommodation.")
        db.add(category)
        db.flush()
    added = 0
    for values in DEMO_ROOMS:
        existing = db.scalar(select(Room.id).where(Room.category_id == category.id, Room.name == values["name"]))
        if existing is not None:
            continue  # Preserve rates, inventory and archive changes made in the admin panel.
        db.add(Room(category_id=category.id,
                    description="Testing only: sample room, illustrative stock photo and fictional nightly rate. Not a real resort offer.",
                    is_active=True, **values))
        added += 1
    db.commit()
    return added


def main():
    if settings.environment not in {"development", "test"}:
        raise SystemExit("Demo seeding is disabled in production.")
    with SessionLocal() as db:
        added = seed_demo_rooms(db, environment=settings.environment)
    print(f"Added {added} demo room types. Existing rooms were left unchanged.")


if __name__ == "__main__":
    main()

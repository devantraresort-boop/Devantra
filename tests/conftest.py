from collections.abc import Generator
from pathlib import Path

import pytest


@pytest.fixture
def test_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session, sessionmaker

    from backend.app.config import settings
    from backend.app.database import Base, build_engine, get_db
    from backend.app.main import app
    from backend.app.models import Admin, Room, RoomCategory
    from backend.app.security import hash_password
    import os
    import uuid
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from backend.app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(settings, "maintenance_secret", "")
    monkeypatch.setattr(settings, "smtp_from_email", "")
    monkeypatch.setattr(settings, "razorpay_key_id", "")
    monkeypatch.setattr(settings, "razorpay_key_secret", "")
    database_path = (tmp_path / "test.sqlite3").as_posix()
    postgres_url = os.environ.get("TEST_DATABASE_URL")
    schema = None
    if postgres_url:
        if not (make_url(postgres_url).database or "").endswith("_test"):
            raise RuntimeError("TEST_DATABASE_URL must point to a disposable database ending in _test")
        raw_engine = build_engine(postgres_url)
        schema = "test_" + uuid.uuid4().hex
        with raw_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        test_engine = raw_engine.execution_options(schema_translate_map={None: schema})
    else:
        raw_engine = test_engine = build_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(bind=test_engine)
    test_sessions = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)

    def override_get_db() -> Generator[Session, None, None]:
        with test_sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(settings, "jwt_secret", "test-only-signing-secret-not-for-production-123")
    with test_sessions() as db:
        category = RoomCategory(name="Test category", description="Created by tests.")
        admin = Admin(name="Test Admin", email="admin@example.com", password_hash=hash_password("test-password-123"))
        db.add_all([category, admin])
        db.flush()
        room = Room(
            category_id=category.id,
            name="Test Room",
            description="Test inventory.",
            base_rate=5000,
            capacity_adults=4,
            capacity_children=2,
            inventory=1,
            amenities="",
            image_url="",
        )
        db.add(room)
        db.commit()
        room_id = room.id

    client = TestClient(app)
    try:
        yield {"client": client, "sessions": test_sessions, "room_id": room_id}
    finally:
        app.dependency_overrides.clear()
        if schema:
            with raw_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        raw_engine.dispose()


@pytest.fixture
def client(test_context):
    return test_context["client"]


@pytest.fixture
def room_id(test_context):
    return test_context["room_id"]


@pytest.fixture
def sessions(test_context):
    return test_context["sessions"]

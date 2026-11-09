from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.config import settings
from backend.app.base import Base

__all__ = ["Base", "build_engine", "engine", "SessionLocal", "get_db"]


def build_engine(database_url: str = settings.database_url):
    connect_args = {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
    options = {}
    if not database_url.startswith("sqlite"):
        connect_args = {"connect_timeout": 10, "prepare_threshold": None}
        if settings.database_null_pool:
            options["poolclass"] = NullPool
        else:
            options.update(pool_size=settings.database_pool_size, max_overflow=settings.database_max_overflow,
                           pool_timeout=15, pool_recycle=300)
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True, **options)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(engine, "begin")
        def begin_immediate(connection):
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    return engine


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session

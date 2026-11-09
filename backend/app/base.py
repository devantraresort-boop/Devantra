"""Shared ORM metadata, independent of runtime environment and database connections."""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass

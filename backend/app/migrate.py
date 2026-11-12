"""Apply migrations once, with a PostgreSQL advisory lock across deploying replicas."""
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from backend.app.config import settings


def main():
    engine = create_engine(settings.migration_database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        if postgres:
            connection.execute(text("SELECT pg_advisory_lock(73493021)"))
            connection.commit()
        try:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        finally:
            if postgres:
                connection.rollback()
                connection.execute(text("SELECT pg_advisory_unlock(73493021)"))
                connection.commit()
    engine.dispose()


if __name__ == "__main__":
    main()

from backend.app.config import Settings


def test_neon_local_env_preserves_defaults_and_selects_direct_migration_url(tmp_path, monkeypatch):
    for name in ("DATABASE_URL", "DATABASE_URL_UNPOOLED", "RESORT_NAME"):
        monkeypatch.delenv(name, raising=False)
    defaults = tmp_path / ".env"
    overrides = tmp_path / ".env.local"
    defaults.write_text("DATABASE_URL=sqlite:///local.db\nRESORT_NAME=Configured Resort\n")
    overrides.write_text(
        "DATABASE_URL=postgresql://user:example@db-pooler.example/test\n"
        "DATABASE_URL_UNPOOLED=postgresql://user:example@db.example/test\n"
    )

    settings = Settings(_env_file=(defaults, overrides))

    assert settings.resort_name == "Configured Resort"
    assert settings.database_url == "postgresql+psycopg://user:example@db-pooler.example/test"
    assert settings.migration_database_url == "postgresql+psycopg://user:example@db.example/test"

    monkeypatch.setenv("DATABASE_URL", "sqlite:///isolated-test.db")
    monkeypatch.setenv("DATABASE_URL_UNPOOLED", "")
    isolated = Settings(_env_file=(defaults, overrides))
    assert isolated.database_url == "sqlite:///isolated-test.db"
    assert isolated.migration_database_url == isolated.database_url

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./devantara.db"
    database_url_unpooled: str = ""
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)
    database_null_pool: bool = False
    jwt_secret: str = ""
    jwt_expire_minutes: int = Field(default=60, ge=5, le=1440)
    resort_timezone: str = "Asia/Kolkata"
    rate_limit_storage_uri: str = "memory://"
    max_request_bytes: int = Field(default=65536, ge=1024, le=1048576)
    maintenance_secret: str = ""
    email_max_attempts: int = Field(default=8, ge=1, le=20)
    cors_origins: list[str] = [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:8765",
        "http://127.0.0.1:8765",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "https://devantraramdevra.in",
        "https://www.devantraramdevra.in",
    ]
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver","devantraramdevra.in","www.devantraramdevra.in",]
    booking_hold_minutes: int = Field(default=15, ge=5, le=60)
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_test_mode: bool = True
    resend_api_key: str = ""
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True
    resort_name: str = "Devantara Resort"
    resort_phone: str = ""
    resort_address: str = ""
    admin_contact_email: str = ""

    @field_validator("database_url", "database_url_unpooled")
    @classmethod
    def postgres_driver(cls, value: str) -> str:
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix):]
        return value

    @property
    def migration_database_url(self) -> str:
        return self.database_url_unpooled or self.database_url

    @field_validator("resort_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def production_configuration(self):
        if self.environment == "production":
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("Production requires PostgreSQL.")
            if len(self.jwt_secret) < 32:
                raise ValueError("Production requires a random JWT_SECRET of at least 32 characters.")
            if not self.allowed_hosts or any("*" in host for host in self.allowed_hosts):
                raise ValueError("Set explicit ALLOWED_HOSTS in production.")
            if any(not origin.startswith("https://") or "*" in origin for origin in self.cors_origins):
                raise ValueError("Production CORS_ORIGINS must be explicit HTTPS origins.")
            if self.smtp_host and not self.smtp_use_tls:
                raise ValueError("SMTP TLS is required in production.")
        if bool(self.razorpay_key_id) != bool(self.razorpay_key_secret):
            raise ValueError("Configure both Razorpay key ID and secret.")
        if self.razorpay_key_id:
            prefix = "rzp_test_" if self.razorpay_test_mode else "rzp_live_"
            if not self.razorpay_key_id.startswith(prefix):
                raise ValueError("Razorpay key does not match RAZORPAY_TEST_MODE.")
            if self.environment == "production" and not self.razorpay_webhook_secret:
                raise ValueError("Configure RAZORPAY_WEBHOOK_SECRET before accepting payments.")
        return self

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

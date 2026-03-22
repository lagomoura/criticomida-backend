from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings

_PLACEHOLDER_SECRET = "change-me-to-a-random-secret-in-production"


class Settings(BaseSettings):
    DATABASE_URL: str = (
        "postgresql+asyncpg://criticomida:criticomida_secret@"
        "localhost:5433/criticomida"
    )
    JWT_SECRET: str = _PLACEHOLDER_SECRET
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "criticomida-api"
    JWT_AUDIENCE: str = "criticomida-clients"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    CORS_ORIGINS: str = "http://localhost:3000"
    APP_ENV: str = "development"
    COOKIE_SECURE: bool = False

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def jwt_secret_strong_when_production(self) -> Self:
        env = self.APP_ENV.strip().lower()
        if env != "production":
            return self
        too_short = len(self.JWT_SECRET) < 32
        is_placeholder = self.JWT_SECRET == _PLACEHOLDER_SECRET
        if too_short or is_placeholder:
            raise ValueError(
                "In production, JWT_SECRET must be at least 32 characters "
                "and must not use the default placeholder value."
            )
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

    @property
    def access_token_max_age_seconds(self) -> int:
        return self.ACCESS_TOKEN_EXPIRE_MINUTES * 60

    @property
    def refresh_token_max_age_seconds(self) -> int:
        return self.REFRESH_TOKEN_EXPIRE_DAYS * 86400


settings = Settings()

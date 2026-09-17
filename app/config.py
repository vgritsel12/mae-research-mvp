from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional during minimal container boot
    load_dotenv = None


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
OUTPUT_DIR = ROOT_DIR / "outputs"

if load_dotenv:
    load_dotenv(ROOT_DIR / ".env")


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _str_env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _path_env(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    app_env: str = field(default_factory=lambda: _str_env("APP_ENV", "local"))
    public_demo: bool = field(default_factory=lambda: _bool_env("PUBLIC_DEMO", False))
    app_password: str = field(default_factory=lambda: _str_env("APP_PASSWORD", ""))
    database_url: str = field(default_factory=lambda: _str_env("DATABASE_URL", ""))
    sqlite_path: Path = field(default_factory=lambda: _path_env("SQLITE_PATH", DATA_DIR / "mae.db"))
    llm_provider: str = field(default_factory=lambda: _str_env("LLM_PROVIDER", "openai"))
    openai_api_key: str = field(default_factory=lambda: _str_env("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: _str_env("OPENAI_MODEL", ""))
    lookback_days: int = field(default_factory=lambda: _int_env("LOOKBACK_DAYS", 90))
    max_articles_per_source: int = field(default_factory=lambda: _int_env("MAX_ARTICLES_PER_SOURCE", 10))
    previous_view_max_days: int = field(default_factory=lambda: _int_env("PREVIOUS_VIEW_MAX_DAYS", 365))
    matrix_max_age_days: int = field(default_factory=lambda: _int_env("MATRIX_MAX_AGE_DAYS", 90))
    http_timeout_seconds: int = field(default_factory=lambda: _int_env("HTTP_TIMEOUT_SECONDS", 20))
    http_connect_timeout_seconds: int = field(default_factory=lambda: _int_env("HTTP_CONNECT_TIMEOUT_SECONDS", 8))
    http_read_timeout_seconds: int = field(default_factory=lambda: _int_env("HTTP_READ_TIMEOUT_SECONDS", 15))
    http_retries: int = field(default_factory=lambda: _int_env("HTTP_RETRIES", 1))
    source_adapter_timeout_seconds: int = field(default_factory=lambda: _int_env("SOURCE_ADAPTER_TIMEOUT_SECONDS", 45))
    collection_wall_clock_seconds: int = field(default_factory=lambda: _int_env("COLLECTION_WALL_CLOCK_SECONDS", 240))
    openai_timeout_seconds: int = field(default_factory=lambda: _int_env("OPENAI_TIMEOUT_SECONDS", 90))
    max_download_mb: int = field(default_factory=lambda: _int_env("MAX_DOWNLOAD_MB", 15))
    max_llm_calls_per_day: int = field(default_factory=lambda: _int_env("MAX_LLM_CALLS_PER_DAY", 20))
    max_llm_calls_per_session: int = field(default_factory=lambda: _int_env("MAX_LLM_CALLS_PER_SESSION", 5))
    max_article_chars: int = field(default_factory=lambda: _int_env("MAX_ARTICLE_CHARS", 30000))
    openai_max_output_tokens: int = field(default_factory=lambda: _int_env("OPENAI_MAX_OUTPUT_TOKENS", 3000))
    log_level: str = field(default_factory=lambda: _str_env("LOG_LEVEL", "INFO"))
    mae_template_csv: Path = field(default_factory=lambda: DATA_DIR / "MAE_template.csv")
    mae_template_xlsx: Path = field(default_factory=lambda: DATA_DIR / "MAE_template_filled.xlsx")
    outlook_sources_path: Path = field(default_factory=lambda: DATA_DIR / "outlook_sources.txt")
    demo_market_series_path: Path = field(default_factory=lambda: ROOT_DIR / "demo_seed" / "market_series.csv")

    @property
    def is_local(self) -> bool:
        return self.app_env == "local"

    @property
    def can_edit_without_password(self) -> bool:
        return self.is_local and not self.public_demo and not self.app_password

    @property
    def database_mode(self) -> str:
        if self.database_url:
            return "DEPLOYED_PERSISTENT"
        if self.public_demo:
            return "READ_ONLY_EPHEMERAL"
        return "LOCAL_PERSISTENT"

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            if self.database_url.startswith("postgres://"):
                return self.database_url.replace("postgres://", "postgresql+psycopg2://", 1)
            if self.database_url.startswith("postgresql://"):
                return self.database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
            return self.database_url
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.sqlite_path}"


def get_settings() -> Settings:
    return Settings()

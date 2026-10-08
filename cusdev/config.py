from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_base_url: str = "http://localhost:8210"
    access_key: str = ""
    timezone: str = "Europe/Moscow"  # даты в именах файлов и на экране
    agent_token: str = ""

    data_dir: Path = Path("./data")
    taxonomy_path: Path = Path("./config/taxonomy.yaml")

    # подписка Claude через CLI Claude Code: `claude setup-token` (ARCHITECTURE.md, Р14)
    claude_code_oauth_token: str = ""
    parse_model: str = "claude-sonnet-5-5"
    batch_model: str = "claude-opus-5-5"

    job_lease_minutes: int = 120
    queue_warn_hours: int = 24
    agent_stale_minutes: int = 10

    # --- агент на Маке (этап 3) ---
    agent_server_url: str = "http://localhost:8210"
    pipeline_dir: Path = Path.home() / "CusDev-Pipeline"
    agent_poll_seconds: int = 30
    # меньше JOB_LEASE_MINUTES: иначе сервер вернёт задачу в очередь раньше, чем агент сдастся
    transcribe_timeout_minutes: int = 110

    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_refresh_token: str = ""
    drive_root_folder_id: str = ""
    index_spreadsheet_id: str = ""

    @property
    def database_path(self) -> Path:
        return self.data_dir / "cusdev.sqlite3"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


@lru_cache
def get_settings() -> Settings:
    return Settings()

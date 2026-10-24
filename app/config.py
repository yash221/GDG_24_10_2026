import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass
class Settings:
    model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    # separate model for photo analysis so it has its own rate-limit bucket
    vision_model: str = os.getenv("VISION_MODEL", "gemini-3.1-flash-lite")
    maps_api_key: str = os.getenv("MAPS_API_KEY", "")
    store: str = os.getenv("STORE", "sqlite")
    db_path: str = os.getenv("DB_PATH", "civic.db")
    gcp_project: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    upload_dir: Path = Path(os.getenv("UPLOAD_DIR", "uploads"))
    admin_token: str = os.getenv("ADMIN_TOKEN", "")
    # behind Cloud Run the real client IP is the last X-Forwarded-For entry
    trust_proxy: bool = os.getenv("TRUST_PROXY", "") == "1"
    allowed_origins: list[str] = field(
        default_factory=lambda: _list(
            os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000")
        )
    )

    max_upload_bytes: int = 8 * 1024 * 1024
    max_image_side: int = 1280
    min_confidence: float = 0.6
    # nearest ward centre further than this means the point is outside AMC limits
    max_ward_distance_km: float = 6.0
    duplicate_radius_m: float = 60.0
    duplicate_window_hours: int = 72

    reports_per_hour: int = 10
    chats_per_hour: int = 30
    sessions_per_hour_per_ip: int = 30
    lookups_per_hour_per_ip: int = 120


settings = Settings()

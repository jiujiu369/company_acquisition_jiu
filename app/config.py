import os
import math
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=False)


@dataclass(frozen=True)
class ScraperAPISettings:
    api_key: str = field(default="", repr=False)
    enabled: bool = False
    timeout: float = 70
    max_attempts: int = 2


def get_scraperapi_settings() -> ScraperAPISettings:
    """All ScraperAPI environment configuration lives here; invalid values keep browser collection usable."""
    try:
        timeout = float(os.getenv("SCRAPERAPI_TIMEOUT", "70"))
        if not math.isfinite(timeout) or timeout <= 0:
            timeout = 70
    except ValueError:
        timeout = 70
    try:
        attempts = max(1, min(3, int(os.getenv("SCRAPERAPI_MAX_ATTEMPTS", "2"))))
    except ValueError:
        attempts = 2
    return ScraperAPISettings(
        api_key=os.getenv("SCRAPERAPI_KEY", "").strip(),
        enabled=os.getenv("SCRAPERAPI_ENABLED", "false").strip().lower() in {"true", "1", "yes", "on"},
        timeout=timeout, max_attempts=attempts,
    )


DATA_SUBDIRS = (
    "products",
    "playwright/screenshots",
    "playwright/pages",
    "temp",
    "exports",
    "database",
)


def get_data_dir() -> Path:
    return Path(os.environ.get("AI_ECOMMERCE_DATA_DIR", "data")).resolve()


def prepare_data_dir(data_dir: Path) -> None:
    for subdir in DATA_SUBDIRS:
        (data_dir / subdir).mkdir(parents=True, exist_ok=True)

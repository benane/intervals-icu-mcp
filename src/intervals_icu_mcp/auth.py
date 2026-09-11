"""Authentication and configuration management for Intervals.icu API."""

import os
from pathlib import Path

from dotenv import load_dotenv, set_key
from pydantic_settings import BaseSettings, SettingsConfigDict


class ICUConfig(BaseSettings):
    """Intervals.icu API configuration from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    intervals_icu_api_key: str = ""
    intervals_icu_athlete_id: str = ""
    intervals_icu_known_athletes: str = ""


def load_config() -> ICUConfig:
    """Load configuration from .env file.

    Returns:
        ICUConfig instance with configuration from environment variables
    """
    load_dotenv()
    return ICUConfig()


def validate_credentials(config: ICUConfig) -> bool:
    """Check if credentials are properly configured.

    Args:
        config: ICUConfig instance to validate

    Returns:
        True if credentials are valid, False otherwise
    """
    if not config.intervals_icu_api_key or config.intervals_icu_api_key == "your_api_key_here":
        return False
    if not config.intervals_icu_athlete_id or config.intervals_icu_athlete_id == "i123456":
        return False
    return True


def parse_known_athletes(raw: str) -> list[tuple[str, str]]:
    """Parse the INTERVALS_ICU_KNOWN_ATHLETES env var into (id, label) pairs.

    Format: comma-separated "id:label" pairs, e.g. "i186312:Benedikt,i222222:Partnerin".

    Args:
        raw: Raw env var value.

    Returns:
        List of (athlete_id, label) tuples. Empty list if raw is empty.
    """
    pairs: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        athlete_id, _, label = entry.partition(":")
        athlete_id = athlete_id.strip()
        label = label.strip() or athlete_id
        if athlete_id:
            pairs.append((athlete_id, label))
    return pairs


def update_env_key(api_key: str, athlete_id: str | None = None) -> None:
    """Update the .env file with new credentials.

    Args:
        api_key: New API key to save
        athlete_id: Optional athlete ID to save
    """
    env_path = Path.cwd() / ".env"

    # Create .env if it doesn't exist
    if not env_path.exists():
        env_path.touch()

    # Update API key
    set_key(str(env_path), "INTERVALS_ICU_API_KEY", api_key)
    os.environ["INTERVALS_ICU_API_KEY"] = api_key

    # Update athlete ID if provided
    if athlete_id:
        set_key(str(env_path), "INTERVALS_ICU_ATHLETE_ID", athlete_id)
        os.environ["INTERVALS_ICU_ATHLETE_ID"] = athlete_id

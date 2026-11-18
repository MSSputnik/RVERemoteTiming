"""Loading of the local configuration file."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

STATIONS = ("start", "3000m", "6000m")
DEFAULT_CONFIG_PATH = Path("config.json")
CONFIG_ENV_VAR = "RVE_TIMING_CONFIG"


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True)
class Config:
    client_id: str
    station: str
    base_url: str
    region: str
    username: str = ""
    timezone: str = "Europe/Berlin"


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH))


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(
            f"Configuration file '{path.resolve()}' not found. "
            "Copy config.example.json to config.json and fill it in."
        ) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read configuration '{path}': {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Configuration must be a JSON object.")

    missing = [
        k for k in ("client_id", "station", "base_url", "region") if not raw.get(k)
    ]
    if missing:
        raise ConfigError(f"Missing configuration value(s): {', '.join(missing)}")

    station = str(raw["station"])
    if station not in STATIONS:
        raise ConfigError(
            f"station must be one of {', '.join(STATIONS)}, got '{station}'"
        )

    return Config(
        client_id=str(raw["client_id"]),
        station=station,
        base_url=str(raw["base_url"]).rstrip("/"),
        region=str(raw["region"]),
        username=str(raw.get("username", "")),
        timezone=str(raw.get("timezone", "Europe/Berlin")),
    )

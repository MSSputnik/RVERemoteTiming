"""Loading of the local configuration file."""

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

STATIONS = ("start", "3000m", "6000m")
DEFAULT_CONFIG_PATH = Path("config.json")
CONFIG_ENV_VAR = "RVE_TIMING_CONFIG"


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


XOR_PREFIX = "{xor}"
XOR_KEY = 0x5F  # same key byte as the WebSphere {xor} encoding
REFRESH_TOKEN_KEY = "refresh_token"


def xor_encode(text: str) -> str:
    """Obfuscate text WebSphere style: XOR every byte, Base64, '{xor}' prefix.

    This is only obfuscation, not encryption.
    """
    data = bytes(b ^ XOR_KEY for b in text.encode("utf-8"))
    return XOR_PREFIX + base64.b64encode(data).decode("ascii")


def xor_decode(value: str) -> str:
    if not value.startswith(XOR_PREFIX):
        raise ValueError("value does not start with {xor}")
    data = base64.b64decode(value[len(XOR_PREFIX) :], validate=True)
    return bytes(b ^ XOR_KEY for b in data).decode("utf-8")


@dataclass(frozen=True)
class Config:
    client_id: str
    station: str
    base_url: str
    region: str
    username: str = ""
    timezone: str = "Europe/Berlin"
    auto_increment: bool = False
    refresh_token: str = ""  # decoded; empty when login is not remembered


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

    auto_increment = raw.get("auto_increment", False)
    if not isinstance(auto_increment, bool):
        raise ConfigError("auto_increment must be true or false.")

    refresh_token = ""
    if raw.get(REFRESH_TOKEN_KEY):
        try:
            refresh_token = xor_decode(str(raw[REFRESH_TOKEN_KEY]))
        except ValueError:
            refresh_token = ""  # corrupt value: ignore, user signs in again

    return Config(
        client_id=str(raw["client_id"]),
        station=station,
        base_url=str(raw["base_url"]).rstrip("/"),
        region=str(raw["region"]),
        username=str(raw.get("username", "")),
        timezone=str(raw.get("timezone", "Europe/Berlin")),
        auto_increment=auto_increment,
        refresh_token=refresh_token,
    )


def save_refresh_token(token: str | None, path: Path | None = None) -> None:
    """Store the refresh token (XOR encoded) in the config file.

    None or an empty string removes it. All other keys are kept.
    """
    path = path or config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if token:
            raw[REFRESH_TOKEN_KEY] = xor_encode(token)
        else:
            raw.pop(REFRESH_TOKEN_KEY, None)
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot write configuration '{path}': {exc}") from exc

"""Loading of the local configuration file."""

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

STATIONS = ("start", "3000m", "6000m")
# The station whose passing times show which boats to expect.
UPSTREAM_STATION = {"3000m": "start", "6000m": "3000m"}
# Not a station: the overview of the Regattaleitung (race info and all stations).
OVERVIEW_STATION = "regattaleitung"
DEFAULT_CONFIG_PATH = Path("config.json")
CONFIG_ENV_VAR = "RVE_TIMING_CONFIG"


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


class ConfigMissing(ConfigError):
    """The configuration file does not exist (yet)."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"Configuration file '{path.resolve()}' not found.")
        self.path = path


# Written when the user lets the app create the configuration file. The login
# parameters are filled in later in the app ("Set Login Parameters ...").
DEFAULT_CONFIG: dict[str, object] = {
    "client_id": "",
    "region": "eu-west-1",
    "station": "start",
    "base_url": "https://langstrecke-api.mssputnik.click",
    "username": "",
    "timezone": "Europe/Berlin",
    "auto_increment": False,
    "show_all_records": False,
    "race_info_interval_minutes": 5,
    "rows_before_focus": 5,
    "show_exit_button": False,
}


def create_default_config(path: Path | None = None) -> Path:
    """Write the configuration file with the default values. Returns its path."""
    path = path or config_path()
    try:
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot create configuration '{path}': {exc}") from exc
    return path


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
    station: str
    client_id: str = ""
    base_url: str = ""
    region: str = ""
    username: str = ""
    timezone: str = "Europe/Berlin"
    auto_increment: bool = False
    show_all_records: bool = False
    race_info_interval_minutes: int = 5  # 0 disables the automatic pull
    rows_before_focus: int = 5  # rows shown above the boat just recorded
    show_exit_button: bool = False  # "Exit" button right of "Sign out"
    refresh_token: str = ""  # decoded; empty when login is not remembered
    # Last file used per upload/download button, e.g. {"download_start": "..."}.
    last_files: dict[str, str] = field(default_factory=dict)


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH))


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigMissing(path) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read configuration '{path}': {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Configuration must be a JSON object.")

    # client_id, region and base_url may be empty: they are set in the app.
    if not raw.get("station"):
        raise ConfigError("Missing configuration value: station")

    station = str(raw["station"])
    if station not in (*STATIONS, OVERVIEW_STATION):
        raise ConfigError(
            f"station must be one of {', '.join((*STATIONS, OVERVIEW_STATION))}, "
            f"got '{station}'"
        )

    timezone = str(raw.get("timezone", "Europe/Berlin"))
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError(f"Unknown timezone '{timezone}'.") from None

    auto_increment = raw.get("auto_increment", False)
    if not isinstance(auto_increment, bool):
        raise ConfigError("auto_increment must be true or false.")

    show_all_records = raw.get("show_all_records", False)
    if not isinstance(show_all_records, bool):
        raise ConfigError("show_all_records must be true or false.")

    race_info_interval_minutes = raw.get("race_info_interval_minutes", 5)
    if (
        isinstance(race_info_interval_minutes, bool)
        or not isinstance(race_info_interval_minutes, int)
        or race_info_interval_minutes < 0
    ):
        raise ConfigError("race_info_interval_minutes must be an integer >= 0.")

    rows_before_focus = raw.get("rows_before_focus", 5)
    if (
        isinstance(rows_before_focus, bool)
        or not isinstance(rows_before_focus, int)
        or rows_before_focus < 0
    ):
        raise ConfigError("rows_before_focus must be an integer >= 0.")

    show_exit_button = raw.get("show_exit_button", False)
    if not isinstance(show_exit_button, bool):
        raise ConfigError("show_exit_button must be true or false.")

    last_files = raw.get("last_files", {})
    if not isinstance(last_files, dict):
        last_files = {}
    last_files = {str(k): str(v) for k, v in last_files.items() if isinstance(v, str)}

    refresh_token = ""
    if raw.get(REFRESH_TOKEN_KEY):
        try:
            refresh_token = xor_decode(str(raw[REFRESH_TOKEN_KEY]))
        except ValueError:
            refresh_token = ""  # corrupt value: ignore, user signs in again

    return Config(
        client_id=str(raw.get("client_id", "")),
        station=station,
        base_url=str(raw.get("base_url", "")).rstrip("/"),
        region=str(raw.get("region", "")),
        username=str(raw.get("username", "")),
        timezone=timezone,
        auto_increment=auto_increment,
        show_all_records=show_all_records,
        race_info_interval_minutes=race_info_interval_minutes,
        rows_before_focus=rows_before_focus,
        show_exit_button=show_exit_button,
        refresh_token=refresh_token,
        last_files=last_files,
    )


def save_refresh_token(token: str | None, path: Path | None = None) -> None:
    """Store the refresh token (XOR encoded) in the config file.

    None or an empty string removes it. All other keys are kept.
    """
    _update_config(REFRESH_TOKEN_KEY, xor_encode(token) if token else None, path)


def save_username(username: str, path: Path | None = None) -> None:
    """Store the user name that was used to sign in."""
    _update_config("username", username, path)


def save_display_settings(
    timezone: str,
    race_info_interval_minutes: int,
    rows_before_focus: int,
    show_exit_button: bool,
    path: Path | None = None,
) -> None:
    """Store the values of the "Settings" dialog of the main window."""
    _update_config_many(
        {
            "timezone": timezone,
            "race_info_interval_minutes": race_info_interval_minutes,
            "rows_before_focus": rows_before_focus,
            "show_exit_button": show_exit_button,
        },
        path,
    )


def save_last_files(last_files: dict[str, str], path: Path | None = None) -> None:
    """Store the last used file of every upload/download button."""
    _update_config("last_files", dict(last_files), path)


def save_auto_increment(value: bool, path: Path | None = None) -> None:
    """Store the state of the "Auto increment" checkbox."""
    _update_config("auto_increment", value, path)


def save_show_all_records(value: bool, path: Path | None = None) -> None:
    """Store the state of the "Show all records" checkbox."""
    _update_config("show_all_records", value, path)


def save_login_parameters(
    client_id: str,
    region: str,
    base_url: str,
    timezone: str,
    station: str,
    path: Path | None = None,
) -> None:
    """Store the values of the "Set Login Parameters" dialog."""
    _update_config_many(
        {
            "client_id": client_id,
            "region": region,
            "base_url": base_url,
            "timezone": timezone,
            "station": station,
        },
        path,
    )


def _update_config(key: str, value: object, path: Path | None) -> None:
    """Set a key in the config file (None removes it), keeping all others."""
    _update_config_many({key: value}, path)


def _update_config_many(updates: dict[str, object], path: Path | None) -> None:
    """Set keys in the config file (None removes one), keeping all others."""
    path = path or config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for key, value in updates.items():
            if value is None:
                raw.pop(key, None)
            else:
                raw[key] = value
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot write configuration '{path}': {exc}") from exc

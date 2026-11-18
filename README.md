# RVE Remote Timing

A small Tkinter desktop app to record passing times at a rowing regatta
station. It talks to the RVEBackend API (see `openapi.json`) and signs in
through Amazon Cognito using boto3.

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- A Cognito app client with the `USER_PASSWORD_AUTH` flow enabled

## Setup

Install the dependencies:

```powershell
uv sync
```

Create the local configuration from the example:

```powershell
Copy-Item config.example.json config.json
```

`config.json` is git-ignored. Set the path in the `RVE_TIMING_CONFIG`
environment variable to use another file.

## Configuration

Required:

- `client_id`: Cognito app client ID.
- `region`: AWS region of the Cognito user pool, for example `eu-central-1`.
- `station`: one of `start`, `3000m`, `6000m`.
- `base_url`: base URL of the API, for example
  `https://langstrecke-api.mssputnik.click`.

Optional:

- `username`: pre-fills the username on the sign-in screen.
- `timezone`: IANA timezone for the displayed times. Default `Europe/Berlin`.

## Run

```powershell
uv run python -m rve_timing
```

## Usage

1. Sign in with your Cognito username and password.
2. Type a boat number and press Enter or click "Record now". The current time
   is sent as the passing time to the configured station.
3. The table shows the recorded timings of the station. Click "Refresh" to
   reload it.

Entries that cannot be sent, for example because of a network problem, are
kept and retried every 10 seconds. The same `entry_id` is reused, so the API
does not store an entry twice. Entries rejected by the server with a 4xx error
are dropped and shown in an error dialog.

## Authentication

The app signs in with `initiate_auth` (`USER_PASSWORD_AUTH`) and refreshes the
tokens with `REFRESH_TOKEN_AUTH` shortly before they expire, or once after a
401 response. The ID token is sent as the Bearer token. No AWS credentials are
needed. Users who must set a new password have to do that outside the app.

## Project layout

| Path | Purpose |
| --- | --- |
| `rve_timing/config.py` | Loads and validates `config.json`. |
| `rve_timing/auth.py` | Cognito sign-in and token refresh. |
| `rve_timing/api.py` | API client. |
| `rve_timing/ui.py` | Tkinter user interface. |
| `rve_timing/version.py` | Application name and version. |
| `rve_timing/__main__.py` | Entry point. |

## Development

```powershell
uv run ruff check .
uv run ruff format .
```

## Versioning

- `APP_VERSION` in `rve_timing/version.py`
- `version` in `pyproject.toml` (then run `uv lock` so `uv.lock` follows)

Whenever the application changes (code or behavior), update the version in both
files. Use semantic versioning: major for breaking changes, minor for new
features, patch for fixes.

The name and version are shown in the window title.

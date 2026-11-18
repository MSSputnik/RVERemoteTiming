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
- `auto_increment`: `true` or `false` (default). When `true`, the boat number
  is increased by one after each "Record now".

## Run

```powershell
uv run python -m rve_timing
```

## Usage

1. Sign in with your Cognito username and password.
2. Type a boat number and press Enter, the `s` key or click "Record now (s)".
   The current time is sent as the passing time to the configured station.
   The arrow buttons next to the field, or the Up and Down keys, change the
   boat number by one without recording it.
3. The table shows all entries of the station, including corrections and
   delete markers (column "Deleted", greyed out). "Current" marks the latest
   entry of each boat. Click "Refresh" to reload it.
4. To correct a time, select an entry and click "Edit time ..." (or double
   click it). Enter the new time as HH:MM:SS. The date stays the same. This
   sends a new entry, the latest one wins.
5. To delete a timing, select an entry and click "Delete" (or press the Delete
   key). This sends a delete marker for the boat. The history is kept, and a
   later normal entry brings the boat back.

Entries that cannot be sent, for example because of a network problem, are
kept and retried every 10 seconds. The same `entry_id` is reused, so the API
does not store an entry twice. Entries rejected by the server with a 4xx error
are dropped and shown in an error dialog.

## Remember login

Tick "Remember login" on the sign-in screen to skip the credentials next
time. The Cognito refresh token is then saved in `config.json` as
`refresh_token`, obfuscated with the WebSphere style `{xor}` encoding (XOR plus
Base64). This is not encryption: anyone who can read `config.json` can recover
the token and sign in as the user. Treat the file like a password.

On start the app uses the stored token to get new tokens. If Cognito rejects
it (expired or revoked), the token is removed and the sign-in screen is shown.
If Cognito cannot be reached, the token is kept. "Sign out" removes it.

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

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
- `region`: AWS region of the Cognito user pool, for example `eu-west-1`.
- `station`: one of `start`, `3000m`, `6000m`.
- `base_url`: base URL of the API, for example
  `https://langstrecke-api.mssputnik.click`.

Optional:

- `username`: pre-fills the username on the sign-in screen.
- `timezone`: IANA timezone, default `Europe/Berlin`. The planned start of the
  race info is taken as a time in this timezone. The passing and recording
  times from the API are converted to it for display. An unknown name stops
  the app at start.
- `auto_increment`: `true` or `false` (default). When `true`, the boat number
  is increased by one after each "Record now". It is the start value of the
  "Auto increment" checkbox. The app writes it back when you toggle the
  checkbox.
- `race_info_interval_minutes`: how often the race info (at `3000m` and
  `6000m`: the timings of the station before) is pulled automatically.
  Default `5`, `0` turns the automatic pull off.
- `show_all_records`: `true` or `false` (default). State of the "Show all
  records" checkbox. The app writes it back when you toggle the checkbox.

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
3. The table lists every boat of the race info, ordered by planned start, with
   the club, the distance and the current passing and recording time. Boats
   without a timing (or whose current entry is a delete marker) have empty
   time columns, so a boat that does not show up keeps its planned slot.
   Boats that have a timing but are not in the race info are highlighted
   in yellow, marked "(not in race info)" and placed by their passing time,
   as if it were the planned start (shown as `~HH:MM:SS`). Without any race
   info the list is a plain list of timings, newest first.
   - The list scrolls so that the boat you just recorded, edited or deleted
     is visible with the 5 boats before it. After a start or "Refresh" it
     scrolls to the first boat whose planned start is not yet over.
   - The race info (or the timings of the station before) is pulled at
     start, on "Refresh" and every
     `race_info_interval_minutes` minutes. The scroll position stays as it is
     on the automatic pull.
   - At the stations `3000m` and `6000m` the list shows which boats to
     expect instead of the race info. At `3000m` these are the boats that
     passed the `start`, at `6000m` the boats that passed `3000m`, in the
     order they passed there (columns Boat, "Start passed" or "3000m passed",
     Passed at, Recorded at). The race info is not used. Boats that did not
     pass your station yet have empty time columns, and the list scrolls to
     the first of them. Boats you recorded that are missing at the station
     before are highlighted and shown behind the boat that passed your
     station last before them. No offset between the stations is calculated.
   - Tick "Show all records" to see every entry instead, including
     corrections and delete markers (greyed out). The columns change to Boat,
     Passed at, Recorded at, Current and Deleted, and the race info is not
     shown. The checkbox state is saved in `config.json`.
4. In the "Show all records" view, the "Restore" button records an older entry
   again. Select it and click "Restore": a new entry with the same boat and
   passing time is sent and becomes the current one. The current entry and
   delete markers cannot be restored, the app shows a message instead.
5. To correct a time, select an entry and click "Edit time ..." (or double
   click it). Enter the new time as HH:MM:SS. The date stays the same. This
   sends a new entry, the latest one wins.
6. To delete a timing, select an entry and click "Delete" (or press the Delete
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

## Linux

On Linux the app uses the `clam` theme and draws the up and down arrows as
images, because many fonts lack the triangle glyphs. Windows keeps its native
look.

Font sizes only work if Tk can use scalable fonts (Xft). The Python that `uv`
downloads has a Tk without Xft, so every text is drawn in one small bitmap
font and the font size cannot change. The app prints a note when it detects
this. For better text, use a Python 3.13 with the system Tk, for example
`python3.13` and `python3.13-tk`, and point uv at it with
`uv sync --python /usr/bin/python3.13`.

If you use the same checkout from Windows and WSL, give each system its own
virtual environment, otherwise `uv` replaces `.venv` on every switch. On the
Linux side set it before `uv run`:

```bash
export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/rve
```

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

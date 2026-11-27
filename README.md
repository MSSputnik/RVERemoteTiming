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

You can also skip this step: when the configuration file does not exist, the
app asks whether to create it with the default values (station `start`, the
default region and base URL, the other options at their defaults). Enter
answers "Yes", ESC answers "No" and the app ends. Set the station in the file
afterwards if the device is not the start, and the client ID in the app with
"Set Login Parameters ...".

`config.json` is git-ignored. Set the path in the `RVE_TIMING_CONFIG`
environment variable to use another file.

## Configuration

Required:

- `station`: one of `start`, `3000m`, `6000m`, or `regattaleitung` for the
  overview of the Regattaleitung (see below).

Set in the app with the "Set Login Parameters ..." button on the sign-in
screen (see below). You can also write them into the file:

- `client_id`: Cognito app client ID.
- `region`: AWS region of the Cognito user pool, for example `eu-west-1`.
- `base_url`: base URL of the API, for example
  `https://langstrecke-api.mssputnik.click`.
- `timezone`: IANA timezone, default `Europe/Berlin`. The planned start of the
  race info is taken as a time in this timezone. The passing and recording
  times from the API are converted to it for display. The "Settings ..."
  button of the main window changes it too.

Optional:

- `username`: pre-fills the username on the sign-in screen. The app saves it
  after each successful sign-in with the password.
- `auto_increment`: `true` or `false` (default). When `true`, the boat number
  is increased by one after each "Record now". It is the start value of the
  "Auto increment" checkbox. The app writes it back when you toggle the
  checkbox.
- `race_info_interval_minutes`: how often the race info (at `3000m` and
  `6000m` also the timings of the station before) is pulled automatically.
  Default `5`, `0` turns the automatic pull off. The "Settings ..." button of
  the main window changes it, and the new interval applies at once.
- `rows_before_focus`: how many rows of the list are shown above the boat you
  just recorded, edited or deleted (and above the first boat due after a
  start or "Refresh"). Default `5`. Use a smaller number on a small screen,
  `0` puts the boat in the first line. The "Settings ..." button of the main
  window changes it too.
- `show_all_records`: `true` or `false` (default). State of the "Show all
  records" checkbox. The app writes it back when you toggle the checkbox.

## Run

```powershell
uv run python -m rve_timing
```

Where a module cannot be run, for example in Pydroid 3 on Android, open
`main.py` in the editor and run it. It only calls the same `main()` as the
module. It changes to its own folder first, so `config.json` is found there
(unless `RVE_TIMING_CONFIG` is set to another file).

For one config file per station, use `run_start.ps1`, `run_3000m.ps1`,
`run_6000m.ps1` or `run_regattaleitung.ps1`. They start the app with
`config_start.json`, `config_3000m.json`, `config_6000m.json` or
`config_regattaleitung.json`. If that file does not exist yet,
`run_station.ps1` creates it from `config.example.json` with the station set
and the username cleared, then you fill in the rest.

## Usage

1. On the first start, click "Set Login Parameters ..." on the sign-in screen
   and enter the client ID, region, base URL and timezone. They are saved in
   `config.json`. Then sign in with your Cognito username and password.
   Changing the client ID or region removes a remembered login, because its
   token belongs to the old user pool. An unknown timezone is rejected.
2. Type a boat number and press Enter, the `s` key or click "Record now (s)".
   The current time is sent as the passing time to the configured station.
   The arrow buttons next to the field, or the Up and Down keys, change the
   boat number by one without recording it. The Tab key moves from the boat
   number to "Record now", "Auto increment", "Show all records", "Restore"
   (when shown), "Edit time", "Delete" and "Refresh", then to the buttons at
   the top ("Settings", "Sign out") and back to the boat number. Shift+Tab
   goes the other way.
   One-letter shortcuts work anywhere in the station view, also in the boat
   number field (the letter is not typed there): `s` records, `i` switches
   "Auto increment", `e` edits the time of the selected entry, `c` changes
   its boat number and `d` deletes it (after a confirmation: Enter
   confirms, ESC cancels). The
   letter is underlined on the button. Upper case works too.
3. The table lists every boat of the race info, ordered by planned start, with
   the club, the distance and the current passing and recording time. Boats
   without a timing (or whose current entry is a delete marker) have empty
   time columns, so a boat that does not show up keeps its planned slot.
   Boats that have a timing but are not in the race info are highlighted
   in yellow, marked "(not in race info)" and placed by their passing time,
   as if it were the planned start (shown as `~HH:MM:SS`). Without any race
   info the list is a plain list of timings, newest first.
   - The list scrolls so that the boat you just recorded, edited or deleted
     is visible with the `rows_before_focus` rows (default 5) before it.
     After a start or "Refresh" it scrolls to the first boat whose planned
     start is not yet over.
   - The race info (and the timings of the station before) is pulled at
     start, on "Refresh" and every
     `race_info_interval_minutes` minutes. The scroll position stays as it is
     on the automatic pull.
   - At the stations `3000m` and `6000m` the list shows which boats to
     expect. At `3000m` these are the boats that passed the `start`, at
     `6000m` the boats that passed `3000m`, in the order they passed there
     (columns Boat, Club, Distance, "Start passed" or "3000m passed",
     Passed at, Recorded at). Club and distance come from the race info;
     boats missing there show "(not in race info)". If the race info cannot
     be loaded, the last one is kept and the error is shown. Boats that did not
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
   "Restore" also brings back a deleted boat, without searching the list.
   The "Deleted" column shows "marker" for delete markers. The "Invalid"
   column shows "yes" for cancelled entries and "marker" for invalidation
   markers (all greyed out).
   - In the same view the "Invalidate" button cancels the selected entry,
     a timing or a delete marker. Invalidating a delete marker undeletes the
     boat. An invalidation marker, or an entry that is already invalid,
     cannot be invalidated: the app shows an error.
   - "Change boat ..." (also in the normal view) corrects a time recorded
     for the wrong boat (shortcut `c`). Select the entry and enter the
     correct boat number. The dialog shows it large, prefilled with the old
     number, with arrow buttons (or the Up and Down keys) to step it:
     the app sends a new entry for that boat with the same passing time and
     an invalidation marker for the selected entry.
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

## Information

The Regattaleitung can publish a short plain text for all stations, for
example its phone number (`GET /information`). The station views
show it in the "Information" box next to "Record passing time" (the row is
split 60 : 40).

- The text is shown in the normal colors. Without a text the box is empty.
- Line breaks are kept. A typed `\n` (backslash, n) is a line break too.
- The box has a fixed height. A longer text gets a scrollbar.
- The app reads the text at sign-in, on "Refresh" and together with the race
  info (every `race_info_interval_minutes` minutes). If it cannot be read,
  the last text stays.

## Regattaleitung

With `"station": "regattaleitung"` the app shows an overview instead of a
station. There is no record box, and times cannot be edited, deleted or
restored. The window opens large and the list uses a larger font.

The list shows every boat of the race info, ordered by planned start, with
the columns Race, Boat, Club, Distance, Planned start and the current passing
time at Start, 3000m and 6000m. An empty cell means the boat has not passed
that station yet, `(deleted)` means its current entry there is a delete
marker. Boats that passed 6000m are shown in grey. Boats with a timing that
are not in the race info are highlighted in yellow and placed by their
passing time at the start. The list scrolls to the boats due now.

The race info and the timings of all three stations are pulled at sign-in, on
"Refresh" and every `race_info_interval_minutes` minutes. The user needs the
group `Regattaleitung` (or `Admin`), which may read all of them.

The buttons next to "Settings ..." handle the files and the information
text of the Regattaleitung:

- "Upload Race Info ..." asks for a CSV file (semicolon separated, header
  `boat_number;planned_start;club;race_number;race_name;distance`) and sends
  it to `PUT /race-info` after a confirmation. Boats missing in the file are
  marked as deleted by the backend. If one row is invalid, nothing is
  written and the error names the row.
- "Download Start ...", "Download 3000m ..." and "Download 6000m ..." ask
  where to save and write the export of that station (`GET /exports/<station>`)
  in `trz` format, with the times in the configured timezone. The file is
  saved as sent, with CRLF line endings.
- "Edit Information ..." loads the current information text and opens it in
  an editor (up to 300 characters, the counter turns red above). "Save" or
  Ctrl+Enter sends it (`POST /information`), ESC cancels. An empty text
  removes the information. The Regattaleitung view itself does not show it.

Each button remembers the last file it used in `config.json` (`last_files`)
and preselects it next time. The first time the names are `race-info.csv`,
`start.trz`, `3000m.trz` and `6000m.trz`.

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
| `rve_timing/__main__.py` | Entry point of `python -m rve_timing`. |
| `main.py` | Launcher script for environments that cannot run a module. |

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

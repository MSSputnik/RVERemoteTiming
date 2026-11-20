"""Tkinter user interface."""

import queue
import re
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, time
from pathlib import Path
from time import monotonic
from tkinter import filedialog, messagebox, ttk
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .api import ApiClient, ApiError
from .auth import AuthUnavailable, CognitoAuth
from .config import (
    OVERVIEW_STATION,
    STATIONS,
    UPSTREAM_STATION,
    Config,
    ConfigError,
    save_auto_increment,
    save_display_settings,
    save_last_files,
    save_login_parameters,
    save_refresh_token,
    save_show_all_records,
    save_username,
)
from .version import APP_NAME, APP_VERSION

RETRY_INTERVAL_MS = 10_000
# The information text of the Regattaleitung (POST /information).
INFORMATION_MAX_LENGTH = 300


@dataclass
class PendingEntry:
    """A timing that could not be sent yet. entry_id is reused on retry."""

    entry_id: uuid.UUID
    boat_number: int
    passed_at: datetime | None
    recorded_at: datetime
    deleted: bool = False


ALL_COLUMNS = ("boat", "passed", "recorded", "current", "deleted")
NORMAL_COLUMNS = ("boat", "club", "distance", "planned", "passed", "recorded")
# 3000m / 6000m: race info, the station before, then this station.
DOWNSTREAM_COLUMNS = ("boat", "club", "distance", "upstream", "passed", "recorded")
# Regattaleitung: race info and the passing time at every station.
OVERVIEW_COLUMNS = (
    "race",
    "boat",
    "club",
    "distance",
    "planned",
    "st_start",
    "st_3000m",
    "st_6000m",
)
# All columns of the list; the view mode picks the visible ones.
TREE_COLUMNS = (
    "boat",
    "planned",
    "club",
    "distance",
    "passed",
    "recorded",
    "current",
    "deleted",
    "upstream",
    "race",
    "st_start",
    "st_3000m",
    "st_6000m",
)
IS_LINUX = sys.platform.startswith("linux")
ROWS_BEFORE_FOCUS = 5  # rows shown above the boat in focus

# Activity indicator (lightning bolt in the bottom right corner).
BOLT_POINTS = (13, 1, 4, 13, 10, 13, 8, 21, 18, 8, 12, 8)
BOLT_ONLINE = "#f5c400"  # yellow: online
BOLT_BUSY = "#b0b0b0"  # gray: dark phase of the flicker
BOLT_OUTLINE = "#808080"
BLINK_INTERVAL_MS = 150
MIN_FLASH_SECONDS = 0.5  # short requests would otherwise not be visible


@dataclass
class Row:
    """One line of the timing list."""

    iid: str
    boat: int
    values: tuple[Any, ...]  # one value per column of the tree
    planned: time | None = None  # planned start; stand-in time for unknown boats
    tags: tuple[str, ...] = ()
    unknown: bool = False  # timing of a boat that is not in the race info
    waiting: bool = False  # expected here (passed the station before), no timing yet


def _arrow_image(up: bool, width: int = 15, height: int = 9) -> tk.PhotoImage:
    """A filled triangle drawn pixel by pixel, independent of any font."""
    image = tk.PhotoImage(width=width, height=height)
    for y in range(height):
        row = y if up else height - 1 - y  # the tip is at row 0
        half = (row * (width - 1)) // (2 * (height - 1))
        mid = width // 2
        image.put("#333333", to=(mid - half, y, mid + half + 1, y + 1))
    return image


def _information_text(text: Any) -> str:
    """Plain text for display. A typed backslash-n is a line break too."""
    return str(text or "").replace("\r\n", "\n").replace("\\n", "\n").strip()


def _disabled(widget: tk.Widget) -> bool:
    """True for a disabled ttk widget (other widgets are never disabled here)."""
    instate = getattr(widget, "instate", None)
    return bool(instate and instate(["disabled"]))


def _parse_planned(value: Any) -> time | None:
    """Parse the planned start (time of day, e.g. 11:02:00) of the race info."""
    try:
        return time.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return None


class App(tk.Tk):
    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config_ = config
        # The Regattaleitung has no station: it only watches and does not record.
        self._overview = config.station == OVERVIEW_STATION
        self._station_timings: dict[str, list[dict[str, Any]]] = {}
        self._status_note = ""  # kept in front of the next overview message
        # Information box of the station view (see _build_information_box).
        self._info_text: tk.Text | None = None
        self._info_loading = False
        self.title(f"{APP_NAME} {APP_VERSION} - {config.station}")
        self._setup_platform_style()
        self._setup_list_style()
        self.minsize(int(600 * self._font_scale), 420)
        if self._overview:
            # A large window: 90 % of the screen height, centered. The width
            # is capped, wider only adds empty space (e.g. ultrawide screens).
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w = min(int(sw * 0.9), int(900 * self._font_scale))
            h = int(sh * 0.9)
            self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")

        # Validated when the configuration is loaded.
        self._tz = ZoneInfo(config.timezone)
        self._remember = False
        self._main_active = False
        self.bind("<Up>", self._key_up)
        self.bind("<Down>", self._key_down)
        # One-letter shortcuts of the station view, lower and upper case.
        self._shortcuts: dict[str, Callable[[], None]] = {
            "s": self._record,
            "i": self._flip_auto_increment,
            "e": self._edit_selected,
            "d": self._delete_selected,
        }
        for letter, action in self._shortcuts.items():
            for key in (f"<{letter}>", f"<{letter.upper()}>"):
                self.bind(key, lambda _e, a=action: self._shortcut(a))
        # Own Tab order in the main view (see _tab_move); dialogs and the
        # sign-in screen keep the default order of Tk.
        self._tab_chain: list[tk.Widget] = []
        self.bind("<Tab>", lambda e: self._tab_move(e, 1))
        self.bind("<<PrevWindow>>", lambda e: self._tab_move(e, -1))
        self._build_clients()
        self._pending: deque[PendingEntry] = deque()
        self._results: queue.Queue[Callable[[], None]] = queue.Queue()
        self._sending = False
        self._arrow_images: list[tk.PhotoImage] = []
        self._race_info: list[dict[str, Any]] = []
        # 3000m and 6000m show the boats of the station before instead of the
        # race info: start -> 3000m -> 6000m.
        self._upstream_station = UPSTREAM_STATION.get(config.station)
        self._upstream: list[dict[str, Any]] = []
        self._upstream_label = (
            self._upstream_station.capitalize() if self._upstream_station else ""
        )
        self._focus_boat: int | None = None  # boat to scroll to on next render
        self._fresh_view = True  # scroll to the boats due now on next render

        self._activity = 0  # requests in flight (Tk thread only)
        self._flash_until = 0.0
        self._blink_on = True

        self._status = tk.StringVar(value="Please sign in.")
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x")
        # The indicator takes the right end, the status line gets the rest.
        self._bolt_canvas = tk.Canvas(
            bottom,
            width=24,
            height=24,
            highlightthickness=0,
            background=ttk.Style(self).lookup("TFrame", "background") or "#f0f0f0",
        )
        self._bolt_canvas.pack(side="right", padx=4)
        self._bolt = self._bolt_canvas.create_polygon(
            *BOLT_POINTS, fill="", outline=BOLT_OUTLINE, width=1
        )
        ttk.Label(bottom, textvariable=self._status, relief="sunken", anchor="w").pack(
            side="left", fill="x", expand=True
        )
        self._container = ttk.Frame(self, padding=10)
        self._container.pack(fill="both", expand=True)

        self.after(100, self._poll_results)
        self.after(BLINK_INTERVAL_MS, self._blink)
        self.after(RETRY_INTERVAL_MS, self._retry_timer)
        self._race_timer_id: str | None = None
        self._schedule_race_timer()
        self._show_login()
        if self._missing_login_parameters():
            self._status.set(
                "Login parameters are missing. Click Set Login Parameters ..."
            )
        elif config.refresh_token:
            self._auto_login(config.refresh_token)

    # ---- helpers -------------------------------------------------------

    def _build_clients(self) -> None:
        """Create the Cognito and API clients from the current configuration."""
        cfg = self.config_
        self._auth = CognitoAuth(cfg.client_id, cfg.region, self._store_refresh_token)
        self._api = ApiClient(cfg.base_url, self._auth)

    def _missing_login_parameters(self) -> list[str]:
        cfg = self.config_
        return [
            name
            for name, value in (
                ("client ID", cfg.client_id),
                ("region", cfg.region),
                ("base URL", cfg.base_url),
            )
            if not value
        ]

    def _run_async(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """Run work in a thread and deliver the result in the Tk thread."""
        self._activity += 1
        self._flash_until = monotonic() + MIN_FLASH_SECONDS

        def target() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - reported to the user
                self._results.put(lambda exc=exc: self._finish(lambda: on_error(exc)))
            else:
                self._results.put(lambda: self._finish(lambda: on_done(result)))

        threading.Thread(target=target, daemon=True).start()

    def _finish(self, callback: Callable[[], None]) -> None:
        self._activity = max(0, self._activity - 1)
        callback()

    def _blink(self) -> None:
        """Flicker the bolt while requests are running, solid otherwise."""
        active = self._activity > 0 or monotonic() < self._flash_until
        self._blink_on = not self._blink_on if active else True
        if self._auth.signed_in:
            fill = BOLT_ONLINE if self._blink_on else BOLT_BUSY
        else:
            # Not signed in: outline only, filled gray while a request runs.
            fill = BOLT_BUSY if active and self._blink_on else ""
        self._bolt_canvas.itemconfigure(self._bolt, fill=fill)
        self.after(BLINK_INTERVAL_MS, self._blink)

    def _poll_results(self) -> None:
        try:
            while True:
                self._results.get_nowait()()
        except queue.Empty:
            pass
        self.after(100, self._poll_results)

    def _now(self) -> datetime:
        return datetime.now(self._tz) if self._tz else datetime.now().astimezone()

    def _store_refresh_token(self, token: str) -> None:
        """Auth callback (may run in a worker thread)."""
        if not self._remember:
            return
        try:
            save_refresh_token(token)
        except ConfigError as exc:
            msg = str(exc)
            self._results.put(lambda: self._status.set(f"Cannot remember login: {msg}"))

    def _forget_refresh_token(self) -> None:
        self._remember = False
        try:
            save_refresh_token(None)
        except ConfigError as exc:
            self._status.set(f"Cannot remove stored login: {exc}")

    def _clear(self) -> None:
        self._main_active = False
        self._tab_chain = []
        for child in self._container.winfo_children():
            child.destroy()

    # ---- login ---------------------------------------------------------

    def _show_login(self) -> None:
        self._clear()
        frame = ttk.Frame(self._container)
        frame.place(relx=0.5, rely=0.4, anchor="center")
        ttk.Label(frame, text="Sign in", font=("TkDefaultFont", 14, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 10)
        )
        ttk.Label(frame, text="Username").grid(
            row=1, column=0, sticky="e", padx=4, pady=4
        )
        ttk.Label(frame, text="Password").grid(
            row=2, column=0, sticky="e", padx=4, pady=4
        )
        self._user_var = tk.StringVar(value=self.config_.username)
        self._pw_var = tk.StringVar()
        user = ttk.Entry(frame, textvariable=self._user_var, width=30)
        pw = ttk.Entry(frame, textvariable=self._pw_var, show="*", width=30)
        user.grid(row=1, column=1, pady=4)
        pw.grid(row=2, column=1, pady=4)
        self._remember_var = tk.BooleanVar(value=bool(self.config_.refresh_token))
        ttk.Checkbutton(frame, text="Remember login", variable=self._remember_var).grid(
            row=3, column=1, sticky="w"
        )
        self._login_btn = ttk.Button(frame, text="Sign in", command=self._do_login)
        self._login_btn.grid(row=4, column=0, columnspan=2, pady=(10, 4))
        ttk.Button(
            frame, text="Set Login Parameters ...", command=self._set_login_parameters
        ).grid(row=5, column=0, columnspan=2, pady=(0, 4))
        for w in (user, pw):
            w.bind("<Return>", lambda _e: self._do_login())
        (pw if self._user_var.get() else user).focus_set()

    def _do_login(self) -> None:
        missing = self._missing_login_parameters()
        if missing:
            self._status.set(
                f"Set the login parameters first (missing: {', '.join(missing)})."
            )
            return
        username, password = self._user_var.get().strip(), self._pw_var.get()
        if not username or not password:
            self._status.set("Enter username and password.")
            return
        self._login_btn.state(["disabled"])
        self._status.set("Signing in ...")
        self._remember = self._remember_var.get()
        if not self._remember:
            self._forget_refresh_token()

        def work() -> dict[str, Any]:
            self._auth.sign_in(username, password)
            return self._api.me()

        def signed_in(me: dict[str, Any]) -> None:
            self._remember_username(username)
            self._login_ok(me)

        self._run_async(work, signed_in, self._login_failed)

    def _remember_username(self, username: str) -> None:
        """Keep the user name in the config, so it is pre-filled next time."""
        if username == self.config_.username:
            return
        try:
            save_username(username)
        except ConfigError as exc:
            self._status.set(f"Cannot save the user name: {exc}")
            return
        self.config_ = replace(self.config_, username=username)

    def _set_login_parameters(self) -> None:
        """Dialog for client ID, region, base URL and timezone; saved in the config."""
        if self._activity > 0:
            self._status.set("Please wait until the running request is finished.")
            return
        values = _ask_login_parameters(self, self.config_)
        if values is None:
            return
        values["base_url"] = values["base_url"].rstrip("/")
        old = self.config_
        try:
            save_login_parameters(
                client_id=values["client_id"],
                region=values["region"],
                base_url=values["base_url"],
                timezone=values["timezone"],
            )
        except ConfigError as exc:
            self._status.set(str(exc))
            return
        self.config_ = replace(
            old,
            client_id=values["client_id"],
            region=values["region"],
            base_url=values["base_url"],
            timezone=values["timezone"],
        )
        self._tz = ZoneInfo(values["timezone"])
        self._build_clients()
        if (values["client_id"], values["region"]) != (old.client_id, old.region):
            # A stored refresh token only works for the user pool it came from.
            self._forget_refresh_token()
            self._remember_var.set(False)
            self.config_ = replace(self.config_, refresh_token="")
        self._status.set("Login parameters saved.")

    def _auto_login(self, refresh_token: str) -> None:
        """Sign in with the stored refresh token, without credentials."""
        self._login_btn.state(["disabled"])
        self._status.set("Signing in with stored login ...")
        self._remember = True

        def work() -> dict[str, Any]:
            self._auth.restore(refresh_token)
            return self._api.me()

        self._run_async(work, self._login_ok, self._auto_login_failed)

    def _auto_login_failed(self, exc: Exception) -> None:
        self._login_btn.state(["!disabled"])
        if isinstance(exc, AuthUnavailable):
            # Token may still be valid: keep it for the next start.
            self._auth.sign_out()
            self._status.set(f"Cannot reach the login service: {exc}")
            return
        self._forget_refresh_token()
        self._remember_var.set(False)
        self._auth.sign_out()
        self._status.set(f"Stored login is no longer valid, please sign in: {exc}")

    def _login_ok(self, me: dict[str, Any]) -> None:
        self._pw_var.set("")
        self._status.set(f"Signed in as {me['username']}.")
        self._show_main(me)

    def _login_failed(self, exc: Exception) -> None:
        self._login_btn.state(["!disabled"])
        self._auth.sign_out()
        self._status.set(f"Sign in failed: {exc}")

    def _sign_out(self) -> None:
        self._forget_refresh_token()
        self._auth.sign_out()
        self._status.set("Signed out.")
        self._show_login()

    # ---- main view -----------------------------------------------------

    def _show_main(self, me: dict[str, Any]) -> None:
        self._clear()
        header = ttk.Frame(self._container)
        header.pack(fill="x")
        groups = ", ".join(me.get("groups", [])) or "no groups"
        ttk.Label(
            header,
            text=f"{self._where()}    User: {me['username']} ({groups})",
        ).pack(side="left")
        sign_out = ttk.Button(header, text="Sign out", command=self._sign_out)
        sign_out.pack(side="right")
        settings = ttk.Button(header, text="Settings ...", command=self._edit_settings)
        settings.pack(side="right", padx=4)
        header_tabs: list[tk.Widget] = []
        if self._overview:
            # Packed from the right: Upload, Download Start, 3000m, 6000m,
            # Edit Information, Settings, Sign out.
            edit_info = ttk.Button(
                header, text="Edit Information ...", command=self._edit_information
            )
            edit_info.pack(side="right", padx=(4, 0))
            downloads: list[tk.Widget] = []
            for station in reversed(STATIONS):
                button = ttk.Button(
                    header,
                    text=f"Download {station.capitalize()} ...",
                    command=lambda s=station: self._download_export(s),
                )
                button.pack(side="right", padx=(4, 0))
                downloads.insert(0, button)
            upload = ttk.Button(
                header, text="Upload Race Info ...", command=self._upload_race_info
            )
            upload.pack(side="right", padx=(4, 0))
            header_tabs = [upload, *downloads, edit_info]
        header_tabs += [settings, sign_out]

        self._main_active = True
        if self._overview:
            body_tabs = self._build_overview_bar()
        else:
            body_tabs = self._build_record_box() + self._build_bar()
        # Tab order: the record box, the bar above the list, then the header
        # from left to right, and back to the boat number.
        self._tab_chain = body_tabs + header_tabs

        # All columns exist; render switches the visible ones per view mode.
        cols = TREE_COLUMNS
        self._tree = ttk.Treeview(
            self._container, columns=cols, show="headings", height=16
        )
        self._column_widths: dict[str, int] = {}  # base width per column
        for col, title, width, anchor in (
            ("boat", "Boat", 70, "center"),
            ("planned", "Planned start", 100, "center"),
            ("club", "Club", 80, "w"),
            ("distance", "Distance", 80, "center"),
            ("passed", "Passed at", 110, "center"),
            ("recorded", "Recorded at", 110, "center"),
            ("current", "Current", 70, "center"),
            ("deleted", "Deleted", 70, "center"),
            ("upstream", f"{self._upstream_label} passed", 120, "center"),
            ("race", "Race", 220, "w"),
            ("st_start", "Start", 100, "center"),
            ("st_3000m", "3000m", 100, "center"),
            ("st_6000m", "6000m", 100, "center"),
        ):
            self._tree.heading(col, text=title)
            self._column_widths[col] = int(width * self._font_scale)
            # stretch=False: _fit_columns sizes the columns, Tk must not.
            self._tree.column(
                col,
                width=self._column_widths[col],
                minwidth=20,
                anchor=anchor,
                stretch=False,
            )
        # Show the columns of the view at once: with all 13 columns the list
        # would ask for a very wide window before the first data arrives.
        show_all = not self._overview and self.config_.show_all_records
        self._tree.configure(displaycolumns=self._view_columns(show_all))
        scroll = ttk.Scrollbar(self._container, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True, pady=(4, 0))
        self._tree.bind("<Configure>", lambda _e: self._fit_columns())
        self._tree.tag_configure("deleted", foreground="#888")
        self._tree.tag_configure("finished", foreground="#777")
        # Alternating row colors; boats missing in the race info are highlighted.
        self._tree.tag_configure("odd", background="#ffffff")
        self._tree.tag_configure("even", background="#eeeeee")
        self._tree.tag_configure("odd_unknown", background="#fff4d6")
        self._tree.tag_configure("even_unknown", background="#ffe9b3")
        if not self._overview:
            self._tree.bind("<Delete>", lambda _e: self._delete_selected())
            self._tree.bind("<Double-1>", lambda _e: self._edit_selected())
        self._items: dict[str, dict[str, Any]] = {}
        self._all_items: list[dict[str, Any]] = []

        self._update_pending_label()
        self._fresh_view = True
        self._refresh_race_info()
        self._refresh()

    def _where(self) -> str:
        if self._overview:
            return "Regattaleitung"
        return f"Station: {self.config_.station}"

    def _build_record_box(self) -> list[tk.Widget]:
        """The box to record a time. Returns its widgets in Tab order.

        It shares one row with the information box, split 60 : 40.
        """
        row = ttk.Frame(self._container)
        row.pack(fill="x", pady=8)
        # uniform: the widths stay strictly 3 : 2, whatever the content needs.
        row.columnconfigure(0, weight=3, uniform="record_row")
        row.columnconfigure(1, weight=2, uniform="record_row")
        entry_frame = ttk.LabelFrame(row, text="Record passing time", padding=8)
        entry_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self._build_information_box(row)
        # Packed first so it takes the bottom strip, right aligned.
        self._auto_inc_var = tk.BooleanVar(value=self.config_.auto_increment)
        auto_inc = ttk.Checkbutton(
            entry_frame,
            text="Auto increment",
            underline=5,  # the "i", shortcut i
            variable=self._auto_inc_var,
            command=self._toggle_auto_increment,
        )
        auto_inc.pack(side="bottom", anchor="e")
        ttk.Label(entry_frame, text="Boat number").pack(side="left")
        self._boat_var = tk.StringVar()
        # Large number; fill="y" stretches the field to the height of the
        # two arrow buttons next to it.
        boat = ttk.Entry(
            entry_frame,
            textvariable=self._boat_var,
            width=6,
            font=("TkDefaultFont", 22),
            justify="center",
        )
        boat.pack(side="left", padx=6, fill="y")
        boat.bind("<Return>", lambda _e: self._record())
        # The entry would insert the typed letter, so the shortcuts are handled
        # here and the key is stopped ("break").
        for letter, action in self._shortcuts.items():
            for key in (f"<{letter}>", f"<{letter.upper()}>"):
                boat.bind(key, lambda _e, a=action: self._shortcut(a) or "break")
        boat.focus_set()
        arrows = ttk.Frame(entry_frame)
        arrows.pack(side="left", padx=(0, 6))
        self._arrow_button(arrows, True, lambda: self._step_boat(1)).pack()
        self._arrow_button(arrows, False, lambda: self._step_boat(-1)).pack()
        record = ttk.Button(entry_frame, text="Record now (s)", command=self._record)
        record.pack(side="left")
        self._main_active = True
        self._pending_var = tk.StringVar()
        ttk.Label(entry_frame, textvariable=self._pending_var, foreground="#b00").pack(
            side="right"
        )
        # The arrow buttons are left out: the Up and Down keys do the same.
        return [boat, record, auto_inc]

    def _build_bar(self) -> list[tk.Widget]:
        """The bar above the list. Returns its widgets in Tab order."""
        bar = ttk.Frame(self._container)
        bar.pack(fill="x")
        ttk.Label(bar, text="Recorded timings").pack(side="left")
        self._show_all_var = tk.BooleanVar(value=self.config_.show_all_records)
        show_all = ttk.Checkbutton(
            bar,
            text="Show all records",
            variable=self._show_all_var,
            command=self._toggle_show_all,
        )
        show_all.pack(side="left", padx=12)
        refresh = ttk.Button(bar, text="Refresh", command=self._refresh_clicked)
        refresh.pack(side="right")
        delete = ttk.Button(
            bar, text="Delete", underline=0, command=self._delete_selected
        )
        delete.pack(side="right", padx=4)
        edit = ttk.Button(
            bar, text="Edit time ...", underline=0, command=self._edit_selected
        )
        edit.pack(side="right")
        # Only shown in the "Show all records" view, see _update_restore_button.
        # While hidden, _tab_move skips it.
        self._restore_btn = ttk.Button(
            bar, text="Restore", command=self._restore_selected
        )
        return [show_all, self._restore_btn, edit, delete, refresh]

    def _build_overview_bar(self) -> list[tk.Widget]:
        """The bar above the overview list. Returns its widgets in Tab order."""
        bar = ttk.Frame(self._container)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Label(bar, text="Overview of all stations").pack(side="left")
        ttk.Label(
            bar,
            text="yellow: not in race info    grey: passed 6000m",
            foreground="#666",
        ).pack(side="left", padx=12)
        refresh = ttk.Button(bar, text="Refresh", command=self._refresh_clicked)
        refresh.pack(side="right")
        return [refresh]

    def _edit_settings(self) -> None:
        """Dialog for the timezone and the race info interval."""
        cfg = self.config_
        values = _ask_fields(
            self,
            "Settings",
            (
                ("timezone", "Timezone (e.g. Europe/Berlin)", cfg.timezone),
                (
                    "interval",
                    "Race info pull interval in minutes (0 = off)",
                    str(cfg.race_info_interval_minutes),
                ),
            ),
            _check_settings,
        )
        if values is None:
            return
        interval = int(values["interval"])
        try:
            save_display_settings(values["timezone"], interval)
        except ConfigError as exc:
            self._status.set(str(exc))
            return
        self.config_ = replace(
            cfg, timezone=values["timezone"], race_info_interval_minutes=interval
        )
        self._tz = ZoneInfo(values["timezone"])
        self._schedule_race_timer()
        self._render_timings()  # times are shown in the new timezone
        self._status.set("Settings saved.")

    def _key_up(self, _event: tk.Event) -> None:
        if self._recording() and self.focus_get() is not self._tree:
            self._step_boat(1)

    def _key_down(self, _event: tk.Event) -> None:
        if self._recording() and self.focus_get() is not self._tree:
            self._step_boat(-1)

    def _tab_move(self, event: tk.Event, step: int) -> str | None:
        """Move the focus along _tab_chain (Tab: step 1, Shift+Tab: step -1).

        Hidden or disabled widgets are skipped. From a widget outside the
        chain (e.g. the list) Tab goes to the start of the chain. Without a
        chain (sign-in screen) Tk handles the key as usual.
        """
        chain = [
            w
            for w in self._tab_chain
            if w.winfo_exists() and w.winfo_ismapped() and not _disabled(w)
        ]
        if not chain:
            return None
        names = [str(w) for w in chain]
        current = str(event.widget)
        if current in names:
            target = chain[(names.index(current) + step) % len(chain)]
        else:
            target = chain[0] if step > 0 else chain[-1]
        # Same as Tk: focus the widget and select the text of an entry.
        self.tk.call("tk::TabToWindow", str(target))
        return "break"

    def _recording(self) -> bool:
        """True while the main view with the record box is shown."""
        return self._main_active and not self._overview

    def _shortcut(self, action: Callable[[], None]) -> None:
        """Run a one-letter shortcut, only in the station view."""
        if self._recording():
            action()

    def _flip_auto_increment(self) -> None:
        """Shortcut i: switch "Auto increment" on or off."""
        self._auto_inc_var.set(not self._auto_inc_var.get())
        self._toggle_auto_increment()

    def _step_boat(self, delta: int) -> None:
        """Change the boat number without recording it."""
        text = self._boat_var.get().strip()
        current = int(text) if text.isdigit() else 0
        self._boat_var.set(str(max(0, current + delta)))

    def _update_pending_label(self) -> None:
        if hasattr(self, "_pending_var"):
            n = len(self._pending)
            self._pending_var.set(f"{n} unsent" if n else "")

    def _record(self) -> None:
        text = self._boat_var.get().strip()
        if not text.isdigit():
            self._status.set("Boat number must be an integer >= 0.")
            return
        now = self._now()
        self._focus_boat = int(text)
        self._pending.append(
            PendingEntry(uuid.uuid4(), int(text), passed_at=now, recorded_at=now)
        )
        self._boat_var.set(str(int(text) + 1) if self._auto_inc_var.get() else "")
        self._update_pending_label()
        self._send_pending()

    def _send_pending(self) -> None:
        if self._sending or not self._pending or not self._auth.signed_in:
            return
        self._sending = True
        entry = self._pending[0]

        def work() -> dict[str, Any]:
            return self._api.post_timing(
                self.config_.station,
                entry.boat_number,
                entry.passed_at,
                entry.entry_id,
                entry.recorded_at,
                entry.deleted,
            )

        self._run_async(
            work,
            lambda r: self._sent_ok(entry, r),
            lambda e: self._sent_failed(entry, e),
        )

    def _sent_ok(self, entry: PendingEntry, result: dict[str, Any]) -> None:
        self._sending = False
        if self._pending and self._pending[0] is entry:
            self._pending.popleft()
        self._update_pending_label()
        if result.get("status") == "duplicate":
            state = "already existed"
        else:
            state = "deleted" if entry.deleted else "saved"
        self._status.set(f"Boat {entry.boat_number}: {state}.")
        self._refresh()
        self._send_pending()

    def _sent_failed(self, entry: PendingEntry, exc: Exception) -> None:
        self._sending = False
        # A 4xx other than auth errors will never succeed: drop and report it.
        if (
            isinstance(exc, ApiError)
            and exc.status
            and 400 <= exc.status < 500
            and exc.status != 401
        ):
            if self._pending and self._pending[0] is entry:
                self._pending.popleft()
            self._update_pending_label()
            messagebox.showerror("Rejected", f"Boat {entry.boat_number}: {exc}")
            self._send_pending()
            return
        self._status.set(f"Boat {entry.boat_number} not sent, will retry: {exc}")
        if isinstance(exc, ApiError) and exc.status == 401 and not self._auth.signed_in:
            self._sign_out_expired()

    def _setup_platform_style(self) -> None:
        """Linux only: use the clam theme; Windows keeps its native look."""
        if not IS_LINUX:
            return
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        if set(tkfont.families(self)) <= {"fixed"}:
            print(
                "Note: this Tk has no scalable fonts (typical for the Python "
                "downloaded by uv), so font sizes cannot change. Use a Python "
                "with the system Tk (python3-tk) for better text.",
                file=sys.stderr,
            )

    def _arrow_button(
        self, parent: tk.Misc, up: bool, command: Callable[[], None]
    ) -> ttk.Button:
        """Up/down button. Linux draws the arrow as an image, because the
        font may lack the triangle glyphs (Windows keeps the text glyph)."""
        if not IS_LINUX:
            return ttk.Button(
                parent, text="\u25b2" if up else "\u25bc", width=3, command=command
            )
        image = _arrow_image(up)
        self._arrow_images.append(image)  # Tk drops images without a reference
        return ttk.Button(parent, image=image, command=command)

    def _setup_list_style(self) -> None:
        """Make the font of the timing list larger than the default.

        2 pt, and 4 pt for the overview of the Regattaleitung.
        """
        delta = 4 if self._overview else 2
        base = tkfont.nametofont("TkDefaultFont")
        size = int(base.cget("size"))
        # A negative size is in pixels: grow it the other way.
        new_size = size + delta if size > 0 else size - delta
        self._list_font = base.copy()  # keep a reference, Tk drops unused fonts
        self._list_font.configure(size=new_size)
        self._font_scale = abs(new_size) / abs(size)
        style = ttk.Style(self)
        style.configure(
            "Treeview",
            font=self._list_font,
            rowheight=self._list_font.metrics("linespace") + 4,
        )
        style.configure("Treeview.Heading", font=self._list_font)

    def _race_interval_ms(self) -> int:
        return self.config_.race_info_interval_minutes * 60_000

    def _schedule_race_timer(self) -> None:
        """(Re)start the automatic pull; an interval of 0 turns it off."""
        if self._race_timer_id is not None:
            self.after_cancel(self._race_timer_id)
            self._race_timer_id = None
        if self.config_.race_info_interval_minutes > 0:
            self._race_timer_id = self.after(self._race_interval_ms(), self._race_timer)

    def _race_timer(self) -> None:
        self._race_timer_id = None
        if self._main_active:
            self._refresh_race_info()
        self._schedule_race_timer()

    def _retry_timer(self) -> None:
        self._send_pending()
        self.after(RETRY_INTERVAL_MS, self._retry_timer)

    def _sign_out_expired(self) -> None:
        self._forget_refresh_token()
        self._auth.sign_out()
        self._show_login()
        self._status.set(
            "Session expired. Please sign in again (unsent entries are kept)."
        )

    def _refresh(self) -> None:
        if not self._auth.signed_in or self._overview:
            return
        self._run_async(
            lambda: self._api.list_timings(self.config_.station),
            self._show_timings,
            lambda e: self._status.set(f"Refresh failed: {e}"),
        )

    def _fmt(self, iso: str) -> str:
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return iso
        if self._tz:
            dt = dt.astimezone(self._tz)
        return dt.strftime("%H:%M:%S")

    def _toggle_auto_increment(self) -> None:
        try:
            save_auto_increment(self._auto_inc_var.get())
        except ConfigError as exc:
            self._status.set(f"Cannot save the setting: {exc}")

    def _toggle_show_all(self) -> None:
        try:
            save_show_all_records(self._show_all_var.get())
        except ConfigError as exc:
            self._status.set(f"Cannot save the setting: {exc}")
        self._fresh_view = True
        self._render_timings()
        self._fresh_view = False

    def _update_restore_button(self, show_all: bool) -> None:
        shown = bool(self._restore_btn.winfo_manager())
        if show_all and not shown:
            self._restore_btn.pack(side="right", padx=4)
        elif not show_all and shown:
            self._restore_btn.pack_forget()

    def _show_timings(self, items: list[dict[str, Any]]) -> None:
        self._all_items = items
        self._render_timings()

    def _refresh_clicked(self) -> None:
        """Refresh button: reload all data and jump to the boats due now."""
        self._fresh_view = True
        self._focus_boat = None
        self._refresh_race_info()
        self._refresh()

    # ---- information of the Regattaleitung -----------------------------

    def _build_information_box(self, row: ttk.Frame) -> None:
        """Calm box with standing information, e.g. the phone number of the
        Regattaleitung (GET /information)."""
        box = ttk.LabelFrame(row, text="Information", padding=8)
        box.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        background = ttk.Style(self).lookup("TFrame", "background") or "#f0f0f0"
        scroll = ttk.Scrollbar(box, orient="vertical")
        # A read-only Text: it wraps at the actual width. width=1 keeps its
        # requested width small, so the 60 : 40 split holds. The height is
        # fixed, a longer text scrolls.
        text = tk.Text(
            box,
            width=1,
            height=3,
            wrap="word",
            font=self._list_font,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=2,
            pady=0,
            background=background,
            cursor="arrow",
            takefocus=0,
        )
        text.pack(side="left", fill="both", expand=True)

        def on_scroll(first: str | float, last: str | float) -> None:
            # The scrollbar is only shown when the text does not fit.
            scroll.set(first, last)
            needed = float(first) > 0 or float(last) < 1
            if needed and not scroll.winfo_manager():
                scroll.pack(side="right", fill="y", before=text)
            elif not needed and scroll.winfo_manager():
                scroll.pack_forget()

        text.configure(yscrollcommand=on_scroll)
        scroll.configure(command=text.yview)
        self._info_text = text
        self._show_information("")

    def _refresh_information(self) -> None:
        box = self._info_text
        if (
            self._info_loading
            or not self._auth.signed_in
            or box is None
            or not box.winfo_exists()
        ):
            return  # no box (Regattaleitung, sign-in screen) or already loading
        self._info_loading = True
        self._run_async(
            self._api.get_information,
            self._information_loaded,
            self._information_failed,
        )

    def _information_loaded(self, data: dict[str, Any]) -> None:
        self._info_loading = False
        self._show_information(_information_text(data.get("text", "")))

    def _information_failed(self, _exc: Exception) -> None:
        # The last text stays and the next pull tries again. No message: the
        # text rarely changes, and the race info pull reports network errors.
        self._info_loading = False

    def _show_information(self, text: str) -> None:
        box = self._info_text
        if box is None or not box.winfo_exists():
            return
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def _edit_information(self) -> None:
        """Regattaleitung: load the current text, edit it, save it."""
        if self._info_loading:
            return
        self._info_loading = True
        self._status.set("Loading the information ...")
        self._run_async(
            self._api.get_information,
            self._information_to_edit,
            self._information_edit_failed,
        )

    def _information_to_edit(self, data: dict[str, Any]) -> None:
        self._info_loading = False
        self._status.set("")
        text = _ask_information(self, str(data.get("text", "")))
        if text is None:
            return
        self._status.set("Saving the information ...")
        self._run_async(
            lambda: self._api.post_information(text),
            lambda result: self._status.set(
                "Information saved." if result.get("text") else "Information removed."
            ),
            lambda e: self._file_action_failed("Edit information", e),
        )

    def _information_edit_failed(self, exc: Exception) -> None:
        self._info_loading = False
        self._file_action_failed("Edit information", exc)

    def _refresh_race_info(self) -> None:
        """Pull the reference data: the race info, at 3000m and 6000m also the
        timings of the station before, and the information text."""
        if not self._auth.signed_in:
            return
        self._refresh_information()
        if self._overview:
            self._run_async(
                self._fetch_overview, self._overview_loaded, self._overview_failed
            )
            return
        if self._upstream_station:
            self._run_async(
                self._fetch_downstream, self._downstream_loaded, self._race_info_failed
            )
            return
        self._run_async(
            self._api.list_race_info, self._race_info_loaded, self._race_info_failed
        )

    def _fetch_overview(self) -> dict[str, Any]:
        """Race info and the timings of all stations (runs in a worker thread)."""
        return {
            "race_info": self._api.list_race_info(),
            "timings": {s: self._api.list_timings(s) for s in STATIONS},
        }

    def _overview_loaded(self, data: dict[str, Any]) -> None:
        self._race_info = data["race_info"]
        self._station_timings = data["timings"]
        boats = sum(1 for r in self._race_info if not r.get("deleted"))
        message = (
            f"Overview updated at {self._now().strftime('%H:%M:%S')}: {boats} boats."
        )
        if self._status_note:
            message = f"{self._status_note} {message}"
            self._status_note = ""
        self._status.set(message)
        self._render_timings()
        self._fresh_view = False

    # ---- Regattaleitung: upload and download ----------------------------

    def _last_file(self, key: str, default_name: str) -> tuple[str, str]:
        """Directory and file name to preselect in the file dialog."""
        last = self.config_.last_files.get(key)
        if last:
            path = Path(last)
            return str(path.parent), path.name
        return str(Path.cwd()), default_name

    def _remember_file(self, key: str, path: str) -> None:
        files = {**self.config_.last_files, key: path}
        try:
            save_last_files(files)
        except ConfigError as exc:
            self._status.set(f"Cannot save the file name: {exc}")
            return
        self.config_ = replace(self.config_, last_files=files)

    def _upload_race_info(self) -> None:
        key = "upload_race_info"
        folder, name = self._last_file(key, "race-info.csv")
        path = filedialog.askopenfilename(
            parent=self,
            title="Upload race info",
            initialdir=folder,
            initialfile=name,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        self._remember_file(key, path)
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            messagebox.showerror("Upload race info", f"Cannot read the file:\n{exc}")
            return
        # OK/Cancel instead of Yes/No: Windows only closes a box with a
        # Cancel button on ESC. Enter presses OK.
        if not messagebox.askokcancel(
            "Upload race info",
            f"Upload {Path(path).name}?\n\n"
            "It replaces the race info. Boats that are missing in the file are "
            "marked as deleted.",
            parent=self,
        ):
            return
        self._status.set(f"Uploading {Path(path).name} ...")
        self._run_async(
            lambda: self._api.put_race_info(data),
            self._race_info_uploaded,
            lambda e: self._file_action_failed("Upload race info", e),
        )

    def _race_info_uploaded(self, result: dict[str, Any]) -> None:
        message = (
            f"Race info uploaded: {result.get('uploaded', 0)} boats, "
            f"{result.get('deleted', 0)} marked as deleted."
        )
        self._status.set(message)
        # Shown again in front of the refresh message, which would hide it.
        self._status_note = message
        self._refresh_race_info()

    def _download_export(self, station: str) -> None:
        key = f"download_{station}"
        folder, name = self._last_file(key, f"{station}.trz")
        path = filedialog.asksaveasfilename(
            parent=self,
            title=f"Download {station}",
            initialdir=folder,
            initialfile=name,
            defaultextension=".trz",
            filetypes=[("TRZ files", "*.trz"), ("All files", "*.*")],
        )
        if not path:
            return
        self._remember_file(key, path)
        tz = self.config_.timezone

        def work() -> int:
            data = self._api.export(station, "trz", tz)
            # Binary: the trz format uses CRLF line endings, keep them as sent.
            Path(path).write_bytes(data)
            return sum(1 for line in data.splitlines() if line.strip())

        self._status.set(f"Downloading {station} ...")
        self._run_async(
            work,
            lambda n: self._status.set(f"{station}: {n} boats saved to {path}."),
            lambda e: self._file_action_failed(f"Download {station}", e),
        )

    def _file_action_failed(self, title: str, exc: Exception) -> None:
        self._status.set(f"{title} failed: {exc}")
        messagebox.showerror(title, str(exc), parent=self)

    def _overview_failed(self, exc: Exception) -> None:
        self._status.set(f"Overview failed: {exc}")
        self._render_timings()
        self._fresh_view = False

    def _fetch_downstream(self) -> dict[str, Any]:
        """Timings of the station before and the race info (worker thread).

        The timings of the station before decide which boats to expect, so
        they must load. The race info only adds club and distance: if it
        fails, the old race info is kept and the error is reported.
        """
        assert self._upstream_station is not None
        upstream = self._api.list_timings(self._upstream_station)
        try:
            return {"upstream": upstream, "race_info": self._api.list_race_info()}
        except ApiError as exc:
            return {"upstream": upstream, "race_info": None, "race_error": exc}

    def _downstream_loaded(self, data: dict[str, Any]) -> None:
        self._upstream = data["upstream"]
        if data["race_info"] is not None:
            self._race_info = data["race_info"]
        boats = sum(1 for i in self._upstream if i["current"] and not i.get("deleted"))
        message = (
            f"{self._upstream_label} timings updated at "
            f"{self._now().strftime('%H:%M:%S')}: {boats} boats."
        )
        if data.get("race_error"):
            message += f" Race info failed: {data['race_error']}"
        self._status.set(message)
        self._render_timings()
        self._fresh_view = False

    def _race_info_loaded(self, items: list[dict[str, Any]]) -> None:
        self._race_info = items
        boats = sum(1 for r in items if not r.get("deleted"))
        self._status.set(
            f"Race info updated at {self._now().strftime('%H:%M:%S')}: {boats} boats."
        )
        self._render_timings()
        self._fresh_view = False

    def _race_info_failed(self, exc: Exception) -> None:
        what = (
            f"{self._upstream_label} timings" if self._upstream_station else "Race info"
        )
        self._status.set(f"{what} failed: {exc}")
        self._render_timings()
        self._fresh_view = False

    def _rows_all(self) -> list[Row]:
        """History view: every entry, newest first, without race info."""
        rows = []
        for item in sorted(
            self._all_items, key=lambda i: i["recorded_at"], reverse=True
        ):
            deleted = bool(item.get("deleted"))
            passed = item.get("passed_at")
            # entry_id is only unique per boat, so the boat is part of the key.
            iid = f"{item['boat_number']}:{item['entry_id']}"
            self._items[iid] = item
            values = (
                item["boat_number"],
                "",
                "",
                "",
                self._fmt(passed) if passed else "-",
                self._fmt(item["recorded_at"]),
                "yes" if item["current"] else "",
                "yes" if deleted else "",
            )
            rows.append(
                Row(
                    iid,
                    item["boat_number"],
                    values,
                    None,
                    ("deleted",) if deleted else (),
                )
            )
        return rows

    def _rows_current(self) -> list[Row]:
        """Race info joined with the current timing of each boat."""
        if self._upstream_station:
            return self._rows_downstream()
        current = {
            i["boat_number"]: i
            for i in self._all_items
            if i["current"] and not i.get("deleted")
        }
        races = sorted(
            (r for r in self._race_info if not r.get("deleted")),
            key=lambda r: (
                _parse_planned(r.get("planned_start")) or time.max,
                r["boat_number"],
            ),
        )
        rows = []
        used: set[int] = set()
        for race in races:
            boat = race["boat_number"]
            item = current.get(boat)
            planned = _parse_planned(race.get("planned_start"))
            if item:
                used.add(boat)
                iid = f"{race['race_number']}:{boat}:{item['entry_id']}"
                self._items[iid] = item
            else:
                iid = f"race:{race['race_number']}:{boat}"
            rows.append(
                Row(iid, boat, self._current_values(boat, race, item, planned), planned)
            )
        extras = [i for b, i in current.items() if b not in used]
        if not races:
            # No race info (not loaded yet or failed): plain list, newest first.
            extras.sort(key=lambda i: i["recorded_at"], reverse=True)
            for item in extras:
                boat = item["boat_number"]
                iid = f"{boat}:{item['entry_id']}"
                self._items[iid] = item
                rows.append(
                    Row(iid, boat, self._current_values(boat, None, item, None))
                )
            return rows
        # Boats that are not in the race info are slotted in by their passing
        # time, as if it were their planned start. Boats that did not show up
        # keep their planned slot with empty time columns.
        for item in extras:
            boat = item["boat_number"]
            iid = f"{boat}:{item['entry_id']}"
            self._items[iid] = item
            stand_in = self._passed_time(item)
            values = list(self._current_values(boat, None, item, None))
            values[1] = f"~{stand_in.strftime('%H:%M:%S')}" if stand_in else ""
            values[2] = "(not in race info)"
            rows.append(Row(iid, boat, tuple(values), stand_in, unknown=True))
        rows.sort(key=lambda r: (r.planned or time.max, r.boat))
        return rows

    def _passed_time(self, item: dict[str, Any]) -> time | None:
        """Time of day of the passing time in the configured timezone."""
        try:
            return datetime.fromisoformat(item["passed_at"]).astimezone(self._tz).time()
        except (KeyError, TypeError, ValueError):
            return None

    def _station_cell(self, item: dict[str, Any] | None) -> str:
        if item is None:
            return ""
        if item.get("deleted"):
            return "(deleted)"
        passed = item.get("passed_at")
        return self._fmt(passed) if passed else "-"

    def _rows_overview(self) -> list[Row]:
        """Regattaleitung: every boat of the race info with its time at each station."""
        current = {
            s: {
                i["boat_number"]: i
                for i in self._station_timings.get(s, [])
                if i["current"]
            }
            for s in STATIONS
        }

        def row(
            iid: str,
            boat: int,
            race: dict[str, Any] | None,
            planned: time | None,
            unknown: bool = False,
        ) -> Row:
            texts = {
                "boat": boat,
                "planned": planned.strftime("%H:%M:%S") if planned and race else "",
                "race": f"{race['race_number']}: {race['race_name']}" if race else "",
                "club": race.get("club", "") if race else "",
                "distance": race.get("distance", "") if race else "",
            }
            for s in STATIONS:
                texts[f"st_{s}"] = self._station_cell(current[s].get(boat))
            if unknown:
                texts["club"] = "(not in race info)"
                texts["planned"] = f"~{planned.strftime('%H:%M:%S')}" if planned else ""
            values = tuple(texts.get(c, "") for c in TREE_COLUMNS)
            last = current["6000m"].get(boat)
            finished = bool(last and not last.get("deleted"))
            return Row(
                iid,
                boat,
                values,
                planned,
                ("finished",) if finished else (),
                unknown=unknown,
            )

        races = sorted(
            (r for r in self._race_info if not r.get("deleted")),
            key=lambda r: (
                _parse_planned(r.get("planned_start")) or time.max,
                r["boat_number"],
            ),
        )
        rows = []
        known: set[int] = set()
        for race in races:
            boat = race["boat_number"]
            known.add(boat)
            planned = _parse_planned(race.get("planned_start"))
            rows.append(row(f"ov:{race['race_number']}:{boat}", boat, race, planned))
        # Boats with a timing that are not in the race info: placed by their
        # passing time at the start, without one they come last.
        timed = {
            b
            for s in STATIONS
            for b, i in current[s].items()
            if not i.get("deleted") and b not in known
        }
        extra = []
        for boat in timed:
            start = current["start"].get(boat)
            stand_in = None
            if start and not start.get("deleted"):
                stand_in = self._passed_time(start)
            extra.append(row(f"ov:?:{boat}", boat, None, stand_in, unknown=bool(races)))
        rows += extra
        rows.sort(key=lambda r: (r.planned or time.max, r.boat))
        return rows

    def _rows_downstream(self) -> list[Row]:
        """3000m / 6000m: boats in the order they passed the station before.

        Boats that did not pass this station yet are the ones to expect. No
        offset is calculated, the order of the station before is the schedule.
        """
        current = {
            i["boat_number"]: i
            for i in self._all_items
            if i["current"] and not i.get("deleted") and i.get("passed_at")
        }
        before = {
            i["boat_number"]: i
            for i in self._upstream
            if i["current"] and not i.get("deleted") and i.get("passed_at")
        }
        # Race info by boat, for club and distance. A boat listed in several
        # races gets the one with the earliest planned start.
        races: dict[int, dict[str, Any]] = {}
        for race in sorted(
            (r for r in self._race_info if not r.get("deleted")),
            key=lambda r: _parse_planned(r.get("planned_start")) or time.max,
        ):
            races.setdefault(race["boat_number"], race)

        def values_for(boat: int, item: dict[str, Any] | None) -> list[Any]:
            values = list(self._current_values(boat, races.get(boat), item, None))
            if races and boat not in races:
                values[2] = "(not in race info)"  # column "club"
            return values

        if not before:
            # Nothing known about the station before (not loaded yet or failed).
            rows = []
            for boat, item in sorted(
                current.items(), key=lambda b: b[1]["recorded_at"], reverse=True
            ):
                iid = f"{boat}:{item['entry_id']}"
                self._items[iid] = item
                rows.append(Row(iid, boat, tuple(values_for(boat, item))))
            return rows

        def passed_dt(item: dict[str, Any]) -> datetime:
            return datetime.fromisoformat(item["passed_at"])

        rows = []
        passed_here: dict[int, datetime] = {}  # boat -> passing time at this station
        for boat, up in sorted(before.items(), key=lambda b: (passed_dt(b[1]), b[0])):
            item = current.get(boat)
            values = values_for(boat, item)
            values.append(self._fmt(up["passed_at"]))  # column "upstream"
            if item:
                iid = f"{boat}:{item['entry_id']}"
                self._items[iid] = item
                passed_here[boat] = passed_dt(item)
            else:
                iid = f"wait:{boat}"
            rows.append(Row(iid, boat, tuple(values), waiting=item is None))
        # Boats with a timing here that did not pass the station before: put
        # them right behind the boat that passed this station last before them.
        extras = [i for b, i in current.items() if b not in before]
        for item in sorted(extras, key=lambda i: (passed_dt(i), i["boat_number"])):
            boat = item["boat_number"]
            iid = f"{boat}:{item['entry_id']}"
            self._items[iid] = item
            values = values_for(boat, item)
            values.append(f"(not at {self._upstream_label})")
            earlier = [
                (when, n)
                for n, r in enumerate(rows)
                if (when := passed_here.get(r.boat)) and when <= passed_dt(item)
            ]
            at = max(earlier)[1] + 1 if earlier else 0
            rows.insert(at, Row(iid, boat, tuple(values), unknown=True))
            passed_here[boat] = passed_dt(item)
        return rows

    def _current_values(
        self,
        boat: int,
        race: dict[str, Any] | None,
        item: dict[str, Any] | None,
        planned: time | None,
    ) -> tuple[Any, ...]:
        passed = item.get("passed_at") if item else None
        return (
            boat,
            planned.strftime("%H:%M:%S") if planned else "",
            race.get("club", "") if race else "",
            race.get("distance", "") if race else "",
            (self._fmt(passed) if passed else "-") if item else "",
            self._fmt(item["recorded_at"]) if item else "",
            "",
            "",
        )

    def _render_timings(self) -> None:
        if not self._tree.winfo_exists():
            return
        show_all = not self._overview and self._show_all_var.get()
        if not self._overview:
            self._update_restore_button(show_all)
        top = self._tree.yview()[0]
        selected = self._tree.selection()
        self._tree.delete(*self._tree.get_children())
        self._items = {}
        columns = self._view_columns(show_all)
        if tuple(self._tree.cget("displaycolumns")) != columns:
            self._tree.configure(displaycolumns=columns)
            self._fit_columns()
        if self._overview:
            rows = self._rows_overview()
        else:
            rows = self._rows_all() if show_all else self._rows_current()
        shown = []
        for row in rows:
            if self._tree.exists(row.iid):  # never fail on a duplicate key
                continue
            stripe = ("even" if len(shown) % 2 else "odd") + (
                "_unknown" if row.unknown else ""
            )
            self._tree.insert(
                "", "end", iid=row.iid, values=row.values, tags=(*row.tags, stripe)
            )
            shown.append(row)
        keep = [i for i in selected if self._tree.exists(i)]
        if keep:
            self._tree.selection_set(keep)
        if self._focus_boat is not None or self._fresh_view:
            self._scroll_to_focus(shown, show_all)
            self._focus_boat = None
        else:
            self._tree.yview_moveto(top)

    def _view_columns(self, show_all: bool) -> tuple[str, ...]:
        """The visible columns of the current view."""
        if self._overview:
            return OVERVIEW_COLUMNS
        if show_all:
            return ALL_COLUMNS
        if self._upstream_station:
            return DOWNSTREAM_COLUMNS
        return NORMAL_COLUMNS

    def _fit_columns(self) -> None:
        """Spread the visible columns over the full width of the list.

        Tk only does this on a resize, so the list would start with a gap on
        the right and keep it after the view switches its columns.
        """
        available = self._tree.winfo_width() - 4  # minus the border
        shown = [c for c in self._tree.cget("displaycolumns") if c != "#all"]
        if available <= 1 or not shown:
            return  # not mapped yet: the first <Configure> calls again
        total = sum(self._column_widths[c] for c in shown)
        used = 0
        for n, col in enumerate(shown):
            if n == len(shown) - 1:
                width = available - used  # the last column takes the rest
            else:
                width = self._column_widths[col] * available // total
            width = max(20, width)
            used += width
            self._tree.column(col, width=width)

    def _scroll_to_focus(self, rows: list[Row], show_all: bool) -> None:
        """Scroll so that the focus row has a few rows above it."""
        if not rows:
            return
        idx = 0 if show_all else self._focus_index(rows)
        self._tree.yview_moveto(max(0, idx - ROWS_BEFORE_FOCUS) / len(rows))

    def _focus_index(self, rows: list[Row]) -> int:
        """Row of the boat just handled, else the first boat due from now on."""
        if self._focus_boat is not None:
            for n, row in enumerate(rows):
                if row.boat == self._focus_boat:
                    return n
        if self._upstream_station:
            # First boat to expect here, i.e. not passed this station yet.
            for n, row in enumerate(rows):
                if row.waiting:
                    return n
            return len(rows) - 1
        now = self._now().time()
        planned = [(n, r.planned) for n, r in enumerate(rows) if r.planned]
        for n, start in planned:
            if start >= now:
                return n
        return planned[-1][0] if planned else 0

    def _selected_item(self) -> dict[str, Any] | None:
        sel = self._tree.selection()
        if not sel:
            self._status.set("Select a timing in the list first.")
            return None
        item = self._items.get(sel[0])
        if item is None:
            self._status.set("No timing recorded for this boat.")
        return item

    def _delete_selected(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        boat = item["boat_number"]
        if item.get("deleted") and item["current"]:
            self._status.set(f"Boat {boat} is already deleted.")
            return
        # OK/Cancel instead of Yes/No: Windows only closes a box with a
        # Cancel button on ESC. Enter presses OK.
        if not messagebox.askokcancel(
            "Delete timing", f"Mark boat {boat} as deleted?", parent=self
        ):
            return
        self._focus_boat = boat
        self._pending.append(
            PendingEntry(
                uuid.uuid4(),
                boat,
                passed_at=None,
                recorded_at=self._now(),
                deleted=True,
            )
        )
        self._update_pending_label()
        self._send_pending()

    def _restore_selected(self) -> None:
        """Record an older entry again: same boat and passing time, new entry."""
        item = self._selected_item()
        if item is None:
            return
        boat = item["boat_number"]
        if item.get("current"):
            messagebox.showinfo(
                "Restore",
                f"This is already the current entry of boat {boat}.\n"
                "Select an older entry to restore it.",
                parent=self,
            )
            return
        if item.get("deleted"):
            messagebox.showinfo(
                "Restore",
                "A delete marker cannot be restored.\n"
                "Select an older timing entry to restore it.",
                parent=self,
            )
            return
        try:
            passed_at = datetime.fromisoformat(item["passed_at"])
        except (KeyError, TypeError, ValueError):
            messagebox.showinfo(
                "Restore", "This entry has no valid passing time.", parent=self
            )
            return
        if passed_at.tzinfo is None:
            passed_at = passed_at.replace(tzinfo=self._tz)
        # A new entry with the latest recorded_at becomes the current one.
        self._focus_boat = boat
        self._pending.append(
            PendingEntry(
                uuid.uuid4(), boat, passed_at=passed_at, recorded_at=self._now()
            )
        )
        self._status.set(
            f"Boat {boat}: restoring {passed_at.astimezone(self._tz).strftime('%H:%M:%S')} ..."
        )
        self._update_pending_label()
        self._send_pending()

    def _edit_selected(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        if not item.get("passed_at"):
            self._status.set("This entry has no passing time to edit.")
            return
        try:
            old = datetime.fromisoformat(item["passed_at"])
        except ValueError:
            self._status.set("Cannot read the passing time of this entry.")
            return
        if self._tz:
            old = old.astimezone(self._tz)
        new = _ask_time(self, item["boat_number"], old)
        if new is None or new == old:
            return
        # A correction is a new entry; the latest recorded_at wins.
        self._focus_boat = item["boat_number"]
        self._pending.append(
            PendingEntry(
                uuid.uuid4(),
                item["boat_number"],
                passed_at=new,
                recorded_at=self._now(),
            )
        )
        self._update_pending_label()
        self._send_pending()


def _ask_fields(
    parent: tk.Tk,
    title: str,
    fields: tuple[tuple[str, str, str], ...],
    check: Callable[[dict[str, str]], str],
) -> dict[str, str] | None:
    """Modal dialog with one text field per (key, label, value).

    check returns a message for the first invalid value, or an empty string.
    Returns None when cancelled.
    """
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.transient(parent)
    dlg.resizable(False, False)
    frame = ttk.Frame(dlg, padding=12)
    frame.pack()
    variables: dict[str, tk.StringVar] = {}
    first: ttk.Entry | None = None
    for row, (key, label, value) in enumerate(fields):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="e", padx=4, pady=4)
        variables[key] = tk.StringVar(value=value)
        entry = ttk.Entry(frame, textvariable=variables[key], width=48)
        entry.grid(row=row, column=1, pady=4)
        first = first or entry
    error = tk.StringVar()
    ttk.Label(frame, textvariable=error, foreground="#b00").grid(
        row=len(fields), column=0, columnspan=2
    )
    result: list[dict[str, str]] = []

    def ok() -> None:
        values = {key: var.get().strip() for key, var in variables.items()}
        problem = check(values)
        if problem:
            error.set(problem)
            return
        result.append(values)
        dlg.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=len(fields) + 1, column=0, columnspan=2, pady=(8, 0))
    ttk.Button(buttons, text="Save", command=ok).pack(side="left", padx=4)
    ttk.Button(buttons, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)
    dlg.bind("<Return>", lambda _e: ok())
    dlg.bind("<Escape>", lambda _e: dlg.destroy())
    if first is not None:
        first.focus_set()
    dlg.grab_set()
    parent.wait_window(dlg)
    return result[0] if result else None


def _ask_information(parent: tk.Tk, text: str) -> str | None:
    """Modal dialog to edit the information text. None when cancelled."""
    dlg = tk.Toplevel(parent)
    dlg.title("Edit information")
    dlg.transient(parent)
    dlg.resizable(False, False)
    frame = ttk.Frame(dlg, padding=12)
    frame.pack()
    ttk.Label(
        frame,
        text="Shown to all stations, e.g. the phone number of the Regattaleitung.\n"
        f"Plain text, up to {INFORMATION_MAX_LENGTH} characters. "
        "Empty removes it.",
    ).grid(row=0, column=0, columnspan=2, sticky="w")
    editor = tk.Text(frame, width=50, height=6, wrap="word", undo=True)
    editor.grid(row=1, column=0, columnspan=2, pady=6)
    editor.insert("1.0", text)
    count = tk.StringVar()
    counter = ttk.Label(frame, textvariable=count)
    counter.grid(row=2, column=0, sticky="w")

    def current() -> str:
        return editor.get("1.0", "end-1c").strip()

    def update_count(_event: tk.Event | None = None) -> None:
        n = len(current())
        count.set(f"{n} / {INFORMATION_MAX_LENGTH} characters")
        counter.configure(foreground="#b00" if n > INFORMATION_MAX_LENGTH else "")
        editor.edit_modified(False)  # re-arm <<Modified>>

    editor.bind("<<Modified>>", update_count)
    update_count()
    result: list[str] = []

    def ok() -> None:
        value = current()
        if len(value) > INFORMATION_MAX_LENGTH:
            dlg.bell()
            return
        result.append(value)
        dlg.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=3, column=0, columnspan=2, pady=(6, 0))
    ttk.Button(buttons, text="Save", command=ok).pack(side="left", padx=4)
    ttk.Button(buttons, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)

    def next_widget(event: tk.Event) -> str:
        # Tab leaves the text instead of typing a tab character.
        target = event.widget.tk_focusNext()
        if target is not None:
            target.focus_set()
        return "break"

    editor.bind("<Tab>", next_widget)
    # Enter makes a new line in the text, Ctrl+Enter saves.
    dlg.bind("<Control-Return>", lambda _e: ok())
    dlg.bind("<Escape>", lambda _e: dlg.destroy())
    editor.focus_set()
    editor.mark_set("insert", "end-1c")
    dlg.grab_set()
    parent.wait_window(dlg)
    return result[0] if result else None


def _ask_login_parameters(parent: tk.Tk, config: Config) -> dict[str, str] | None:
    """Modal dialog for the connection settings. None when cancelled."""
    return _ask_fields(
        parent,
        "Login parameters",
        (
            ("client_id", "Client ID", config.client_id),
            ("region", "Region (e.g. eu-west-1)", config.region),
            ("base_url", "Base URL", config.base_url),
            ("timezone", "Timezone (e.g. Europe/Berlin)", config.timezone),
        ),
        _check_login_parameters,
    )


def _check_settings(values: dict[str, str]) -> str:
    """Validate the timezone and the pull interval of the settings dialog."""
    try:
        ZoneInfo(values["timezone"])
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return f"Unknown timezone {values['timezone']}."
    if not re.fullmatch(r"\d{1,4}", values["interval"]):
        return "The interval is a whole number of minutes, 0 to 9999."
    return ""


def _check_login_parameters(values: dict[str, str]) -> str:
    """Return a message for the first invalid value, or an empty string."""
    if not values["client_id"]:
        return "Enter the client ID."
    if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", values["region"]):
        return "The region looks like eu-west-1."
    if not re.fullmatch(r"https?://[^\s/]+(/\S*)?", values["base_url"]):
        return "The base URL starts with https:// (or http://)."
    try:
        ZoneInfo(values["timezone"])
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return f"Unknown timezone {values['timezone']}."
    return ""


def _parse_time(text: str, base: datetime) -> datetime | None:
    """Parse HH:MM:SS[.fff] on the date and timezone of base."""
    try:
        t = time.fromisoformat(text.strip())
    except ValueError:
        return None
    return datetime.combine(base.date(), t, tzinfo=base.tzinfo)


def _ask_time(parent: tk.Tk, boat: int, old: datetime) -> datetime | None:
    """Modal dialog to change a passing time. Returns None when cancelled."""
    dlg = tk.Toplevel(parent)
    dlg.title(f"Edit time - boat {boat}")
    dlg.transient(parent)
    dlg.resizable(False, False)
    frame = ttk.Frame(dlg, padding=12)
    frame.pack()
    ttk.Label(frame, text=f"Passed at (HH:MM:SS), date {old.date().isoformat()}").grid(
        row=0, column=0, columnspan=2, pady=(0, 6)
    )
    var = tk.StringVar(value=old.strftime("%H:%M:%S"))
    entry = ttk.Entry(frame, textvariable=var, width=14, justify="center")
    entry.grid(row=1, column=0, columnspan=2, pady=4)
    error = tk.StringVar()
    ttk.Label(frame, textvariable=error, foreground="#b00").grid(
        row=2, column=0, columnspan=2
    )
    result: list[datetime] = []

    def ok() -> None:
        new = _parse_time(var.get(), old)
        if new is None:
            error.set("Use the format HH:MM:SS")
            return
        result.append(new)
        dlg.destroy()

    ttk.Button(frame, text="Update", command=ok).grid(row=3, column=0, pady=8)
    ttk.Button(frame, text="Cancel", command=dlg.destroy).grid(row=3, column=1)
    dlg.bind("<Return>", lambda _e: ok())
    dlg.bind("<Escape>", lambda _e: dlg.destroy())
    entry.focus_set()
    entry.select_range(0, "end")
    dlg.grab_set()
    parent.wait_window(dlg)
    return result[0] if result else None


def run(config: Config) -> None:
    App(config).mainloop()

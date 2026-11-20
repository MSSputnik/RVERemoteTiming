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
from time import monotonic
from tkinter import messagebox, ttk
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
    save_login_parameters,
    save_refresh_token,
    save_show_all_records,
    save_username,
)
from .version import APP_NAME, APP_VERSION

RETRY_INTERVAL_MS = 10_000


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
        self.title(f"{APP_NAME} {APP_VERSION} - {config.station}")
        self._setup_platform_style()
        self._setup_list_style()
        self.minsize(int(760 * self._font_scale), 420)
        if self._overview:
            # A large window: 90 % of the screen height, centered. The width
            # is capped, wider only adds empty space (e.g. ultrawide screens).
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w = min(int(sw * 0.9), int(1400 * self._font_scale))
            h = int(sh * 0.9)
            self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")

        # Validated when the configuration is loaded.
        self._tz = ZoneInfo(config.timezone)
        self._remember = False
        self._main_active = False
        self.bind("<Up>", self._key_up)
        self.bind("<Down>", self._key_down)
        self.bind("<s>", self._key_s)
        self.bind("<S>", self._key_s)
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
        ttk.Button(header, text="Sign out", command=self._sign_out).pack(side="right")
        ttk.Button(header, text="Settings ...", command=self._edit_settings).pack(
            side="right", padx=4
        )

        self._main_active = True
        if self._overview:
            self._build_overview_bar()
        else:
            self._build_record_box()
            self._build_bar()

        # All columns exist; render switches the visible ones per view mode.
        cols = TREE_COLUMNS
        self._tree = ttk.Treeview(
            self._container, columns=cols, show="headings", height=16
        )
        for col, title, width in (
            ("boat", "Boat", 70),
            ("planned", "Planned start", 100),
            ("club", "Club", 180),
            ("distance", "Distance", 80),
            ("passed", "Passed at", 110),
            ("recorded", "Recorded at", 110),
            ("current", "Current", 70),
            ("deleted", "Deleted", 70),
            ("upstream", f"{self._upstream_label} passed", 120),
            ("race", "Race", 220),
            ("st_start", "Start", 100),
            ("st_3000m", "3000m", 100),
            ("st_6000m", "6000m", 100),
        ):
            self._tree.heading(col, text=title)
            self._tree.column(col, width=int(width * self._font_scale), anchor="center")
        scroll = ttk.Scrollbar(self._container, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True, pady=(4, 0))
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

    def _build_record_box(self) -> None:
        entry_frame = ttk.LabelFrame(
            self._container, text="Record passing time", padding=8
        )
        entry_frame.pack(fill="x", pady=8)
        # Packed first so it takes the bottom strip, right aligned.
        self._auto_inc_var = tk.BooleanVar(value=self.config_.auto_increment)
        ttk.Checkbutton(
            entry_frame,
            text="Auto increment",
            variable=self._auto_inc_var,
            command=self._toggle_auto_increment,
        ).pack(side="bottom", anchor="e")
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
        # The entry would insert the typed letter, so handle s here and stop it.
        for key in ("<s>", "<S>"):
            boat.bind(key, lambda _e: self._record_key())
        boat.focus_set()
        arrows = ttk.Frame(entry_frame)
        arrows.pack(side="left", padx=(0, 6))
        self._arrow_button(arrows, True, lambda: self._step_boat(1)).pack()
        self._arrow_button(arrows, False, lambda: self._step_boat(-1)).pack()
        ttk.Button(entry_frame, text="Record now (s)", command=self._record).pack(
            side="left"
        )
        self._main_active = True
        self._pending_var = tk.StringVar()
        ttk.Label(entry_frame, textvariable=self._pending_var, foreground="#b00").pack(
            side="right"
        )

    def _build_bar(self) -> None:
        bar = ttk.Frame(self._container)
        bar.pack(fill="x")
        ttk.Label(bar, text="Recorded timings").pack(side="left")
        self._show_all_var = tk.BooleanVar(value=self.config_.show_all_records)
        ttk.Checkbutton(
            bar,
            text="Show all records",
            variable=self._show_all_var,
            command=self._toggle_show_all,
        ).pack(side="left", padx=12)
        ttk.Button(bar, text="Refresh", command=self._refresh_clicked).pack(
            side="right"
        )
        ttk.Button(bar, text="Delete", command=self._delete_selected).pack(
            side="right", padx=4
        )
        ttk.Button(bar, text="Edit time ...", command=self._edit_selected).pack(
            side="right"
        )
        # Only shown in the "Show all records" view, see _update_restore_button.
        self._restore_btn = ttk.Button(
            bar, text="Restore", command=self._restore_selected
        )

    def _build_overview_bar(self) -> None:
        bar = ttk.Frame(self._container)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Label(bar, text="Overview of all stations").pack(side="left")
        ttk.Label(
            bar,
            text="yellow: not in race info    grey: passed 6000m",
            foreground="#666",
        ).pack(side="left", padx=12)
        ttk.Button(bar, text="Refresh", command=self._refresh_clicked).pack(
            side="right"
        )

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

    def _recording(self) -> bool:
        """True while the main view with the record box is shown."""
        return self._main_active and not self._overview

    def _key_s(self, _event: tk.Event) -> None:
        if self._recording():
            self._record()

    def _record_key(self) -> str:
        self._record()
        return "break"

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

    def _refresh_race_info(self) -> None:
        """Pull the reference data: the race info, at 3000m and 6000m also the
        timings of the station before."""
        if not self._auth.signed_in:
            return
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
        self._status.set(
            f"Overview updated at {self._now().strftime('%H:%M:%S')}: {boats} boats."
        )
        self._render_timings()
        self._fresh_view = False

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
        if self._overview:
            columns = OVERVIEW_COLUMNS
        elif show_all:
            columns = ALL_COLUMNS
        elif self._upstream_station:
            columns = DOWNSTREAM_COLUMNS
        else:
            columns = NORMAL_COLUMNS
        self._tree.configure(displaycolumns=columns)
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
        if not messagebox.askyesno(
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

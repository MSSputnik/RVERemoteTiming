"""Tkinter user interface."""

import queue
import threading
import tkinter as tk
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .api import ApiClient, ApiError
from .auth import AuthUnavailable, CognitoAuth
from .config import Config, ConfigError, save_refresh_token
from .version import APP_NAME, APP_VERSION

RETRY_INTERVAL_MS = 10_000


@dataclass
class PendingEntry:
    """A timing that could not be sent yet. entry_id is reused on retry."""

    entry_id: uuid.UUID
    boat_number: int
    passed_at: datetime
    recorded_at: datetime


class App(tk.Tk):
    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config_ = config
        self.title(f"{APP_NAME} {APP_VERSION} - {config.station}")
        self.minsize(520, 420)

        try:
            self._tz = ZoneInfo(config.timezone)
        except ZoneInfoNotFoundError:
            self._tz = None
        self._remember = False
        self._auth = CognitoAuth(
            config.client_id, config.region, self._store_refresh_token
        )
        self._api = ApiClient(config.base_url, self._auth)
        self._pending: deque[PendingEntry] = deque()
        self._results: queue.Queue[Callable[[], None]] = queue.Queue()
        self._sending = False

        self._status = tk.StringVar(value="Please sign in.")
        ttk.Label(self, textvariable=self._status, relief="sunken", anchor="w").pack(
            side="bottom", fill="x"
        )
        self._container = ttk.Frame(self, padding=10)
        self._container.pack(fill="both", expand=True)

        self.after(100, self._poll_results)
        self.after(RETRY_INTERVAL_MS, self._retry_timer)
        self._show_login()
        if config.refresh_token:
            self._auto_login(config.refresh_token)

    # ---- helpers -------------------------------------------------------

    def _run_async(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """Run work in a thread and deliver the result in the Tk thread."""

        def target() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - reported to the user
                self._results.put(lambda exc=exc: on_error(exc))
            else:
                self._results.put(lambda: on_done(result))

        threading.Thread(target=target, daemon=True).start()

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
        self._login_btn.grid(row=4, column=0, columnspan=2, pady=10)
        for w in (user, pw):
            w.bind("<Return>", lambda _e: self._do_login())
        (pw if self._user_var.get() else user).focus_set()

    def _do_login(self) -> None:
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

        self._run_async(work, self._login_ok, self._login_failed)

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
            text=f"Station: {self.config_.station}    User: {me['username']} ({groups})",
        ).pack(side="left")
        ttk.Button(header, text="Sign out", command=self._sign_out).pack(side="right")

        entry_frame = ttk.LabelFrame(
            self._container, text="Record passing time", padding=8
        )
        entry_frame.pack(fill="x", pady=8)
        ttk.Label(entry_frame, text="Boat number").pack(side="left")
        self._boat_var = tk.StringVar()
        boat = ttk.Entry(entry_frame, textvariable=self._boat_var, width=10)
        boat.pack(side="left", padx=6)
        boat.bind("<Return>", lambda _e: self._record())
        boat.focus_set()
        ttk.Button(entry_frame, text="Record now", command=self._record).pack(
            side="left"
        )
        self._pending_var = tk.StringVar()
        ttk.Label(entry_frame, textvariable=self._pending_var, foreground="#b00").pack(
            side="right"
        )

        bar = ttk.Frame(self._container)
        bar.pack(fill="x")
        ttk.Label(bar, text="Recorded timings").pack(side="left")
        ttk.Button(bar, text="Refresh", command=self._refresh).pack(side="right")

        cols = ("boat", "passed", "recorded", "current")
        self._tree = ttk.Treeview(
            self._container, columns=cols, show="headings", height=12
        )
        for col, title, width in (
            ("boat", "Boat", 70),
            ("passed", "Passed at", 160),
            ("recorded", "Recorded at", 160),
            ("current", "Current", 70),
        ):
            self._tree.heading(col, text=title)
            self._tree.column(col, width=width, anchor="center")
        scroll = ttk.Scrollbar(self._container, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True, pady=(4, 0))

        self._update_pending_label()
        self._refresh()

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
        self._pending.append(
            PendingEntry(uuid.uuid4(), int(text), passed_at=now, recorded_at=now)
        )
        self._boat_var.set("")
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
        state = "already existed" if result.get("status") == "duplicate" else "saved"
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
        if not self._auth.signed_in:
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

    def _show_timings(self, items: list[dict[str, Any]]) -> None:
        if not self._tree.winfo_exists():
            return
        self._tree.delete(*self._tree.get_children())
        for item in sorted(items, key=lambda i: i["recorded_at"], reverse=True):
            self._tree.insert(
                "",
                "end",
                values=(
                    item["boat_number"],
                    self._fmt(item["passed_at"]),
                    self._fmt(item["recorded_at"]),
                    "yes" if item["current"] else "",
                ),
            )


def run(config: Config) -> None:
    App(config).mainloop()

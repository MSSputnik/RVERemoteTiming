"""Client for the RVEBackend API (see openapi.json)."""

import uuid
from datetime import datetime
from typing import Any

import requests

from .auth import AuthError, CognitoAuth

TIMEOUT = 15


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ApiClient:
    def __init__(self, base_url: str, auth: CognitoAuth) -> None:
        self._base_url = base_url
        self._auth = auth
        self._session = requests.Session()

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        for attempt in (1, 2):
            try:
                token = self._auth.get_id_token()
            except AuthError as exc:
                raise ApiError(f"Authentication failed: {exc}", 401) from exc
            try:
                resp = self._session.request(
                    method,
                    self._base_url + path,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=TIMEOUT,
                    **kwargs,
                )
            except requests.RequestException as exc:
                raise ApiError(f"Network error: {exc}") from exc
            if resp.status_code == 401 and attempt == 1:
                # Token may have been rejected early: refresh once and retry.
                try:
                    self._auth.refresh()
                except AuthError as exc:
                    raise ApiError(f"Session expired: {exc}", 401) from exc
                continue
            break
        if not resp.ok:
            raise ApiError(_error_text(resp), resp.status_code)
        return resp

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/me").json()

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/status").json()

    def list_timings(self, station: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/timings/{station}").json()["items"]

    def post_timing(
        self,
        station: str,
        boat_number: int,
        passed_at: datetime | None,
        entry_id: uuid.UUID | None = None,
        recorded_at: datetime | None = None,
        deleted: bool = False,
    ) -> dict[str, Any]:
        """Record a passing time, or a delete marker with deleted=True.

        Reuse entry_id when retrying (idempotent). A correction is a new
        entry: the latest recorded_at wins.
        """
        if passed_at is None and not deleted:
            raise ValueError("passed_at is required unless deleted is true")
        if passed_at is not None and passed_at.tzinfo is None:
            raise ValueError("passed_at needs a timezone")
        recorded_at = recorded_at or datetime.now().astimezone()
        body: dict[str, Any] = {
            "entry_id": str(entry_id or uuid.uuid4()),
            "boat_number": boat_number,
            "recorded_at": recorded_at.isoformat(),
        }
        if passed_at is not None:
            body["passed_at"] = passed_at.isoformat()
        if deleted:
            body["deleted"] = True
        return self._request("POST", f"/timings/{station}", json=body).json()

    def list_race_info(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        params = {"includeDeleted": "true"} if include_deleted else None
        return self._request("GET", "/race-info", params=params).json()["items"]


def _error_text(resp: requests.Response) -> str:
    try:
        detail = resp.json().get("error")
    except ValueError:
        detail = None
    return f"HTTP {resp.status_code}: {detail or resp.reason}"

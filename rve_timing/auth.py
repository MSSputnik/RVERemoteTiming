"""Cognito authentication and token refresh using the plain Cognito HTTP API."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

# Refresh this many seconds before the token really expires.
EXPIRY_MARGIN = 60

# Timeout in seconds for calls to Cognito.
REQUEST_TIMEOUT = 15


class AuthError(Exception):
    """Raised when sign in or refresh fails."""


class AuthUnavailable(AuthError):
    """Cognito could not be reached. The session is still valid."""


class NewPasswordRequired(AuthError):
    """The user must set a new password first (done in the Cognito UI)."""


@dataclass
class Tokens:
    id_token: str
    access_token: str
    refresh_token: str
    expires_at: float


class CognitoAuth:
    """Sign in with USER_PASSWORD_AUTH and keep the tokens fresh.

    The API Gateway JWT authorizer is fed with the ID token (it carries the
    audience and the cognito:groups claim).
    """

    def __init__(
        self,
        client_id: str,
        region: str,
        on_refresh_token: Callable[[str], None] | None = None,
    ) -> None:
        self._client_id = client_id
        # Called with the refresh token after every sign in / refresh.
        self._on_refresh_token = on_refresh_token
        self._region = region
        self._tokens: Tokens | None = None
        self._lock = threading.Lock()

    def _initiate_auth(
        self, auth_flow: str, auth_parameters: dict[str, str]
    ) -> dict[str, Any]:
        """Call Cognito InitiateAuth. Public app client calls are unsigned.

        Raises AuthError when Cognito rejects the request and AuthUnavailable
        when it cannot be reached.
        """
        # Built per call, so an empty or wrong region does not stop the app
        # from starting. It is reported when signing in.
        url = f"https://cognito-idp.{self._region}.amazonaws.com/"
        try:
            resp = requests.post(
                url,
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
                },
                json={
                    "ClientId": self._client_id,
                    "AuthFlow": auth_flow,
                    "AuthParameters": auth_parameters,
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise AuthUnavailable(str(exc)) from exc

        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200:
            message = (
                body.get("message")
                or body.get("Message")
                or f"Cognito error (HTTP {resp.status_code})."
            )
            if resp.status_code >= 500:
                # Server side problem: not a rejection, the session stays valid.
                raise AuthUnavailable(message)
            raise AuthError(message)
        return body

    @property
    def signed_in(self) -> bool:
        return self._tokens is not None

    def sign_in(self, username: str, password: str) -> None:
        try:
            resp = self._initiate_auth(
                "USER_PASSWORD_AUTH", {"USERNAME": username, "PASSWORD": password}
            )
        except AuthUnavailable as exc:
            # Sign in has no session to keep, so report it as a plain error.
            raise AuthError(str(exc)) from exc

        if "AuthenticationResult" not in resp:
            challenge = resp.get("ChallengeName", "unknown")
            if challenge == "NEW_PASSWORD_REQUIRED":
                raise NewPasswordRequired("A new password must be set for this user.")
            raise AuthError(f"Unsupported authentication challenge: {challenge}")

        result = resp["AuthenticationResult"]
        with self._lock:
            self._tokens = Tokens(
                id_token=result["IdToken"],
                access_token=result["AccessToken"],
                refresh_token=result["RefreshToken"],
                expires_at=time.time() + result["ExpiresIn"],
            )
        self._notify(result["RefreshToken"])

    def restore(self, refresh_token: str) -> None:
        """Sign in with a stored refresh token. Raises AuthError if invalid."""
        with self._lock:
            self._tokens = Tokens("", "", refresh_token, 0.0)
        self.refresh()

    def _notify(self, refresh_token: str) -> None:
        if self._on_refresh_token:
            self._on_refresh_token(refresh_token)

    def refresh(self) -> None:
        with self._lock:
            if self._tokens is None:
                raise AuthError("Not signed in.")
            refresh_token = self._tokens.refresh_token
            try:
                resp = self._initiate_auth(
                    "REFRESH_TOKEN_AUTH", {"REFRESH_TOKEN": refresh_token}
                )
            except AuthUnavailable:
                # Connection problem: keep the tokens and let the caller retry.
                raise
            except AuthError:
                # Refresh token rejected (expired/revoked): session is over.
                self._tokens = None
                raise
            result = resp["AuthenticationResult"]
            # A refresh does not return a new refresh token unless rotation is on.
            self._tokens = Tokens(
                id_token=result["IdToken"],
                access_token=result["AccessToken"],
                refresh_token=result.get("RefreshToken", refresh_token),
                expires_at=time.time() + result["ExpiresIn"],
            )
            new_refresh_token = self._tokens.refresh_token
        self._notify(new_refresh_token)

    def get_id_token(self) -> str:
        """Return a valid ID token, refreshing it when it is about to expire."""
        tokens = self._tokens
        if tokens is None:
            raise AuthError("Not signed in.")
        if tokens.expires_at - time.time() < EXPIRY_MARGIN:
            self.refresh()
            tokens = self._tokens
            assert tokens is not None
        return tokens.id_token

    def sign_out(self) -> None:
        with self._lock:
            self._tokens = None

"""Cognito authentication and token refresh using boto3."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import boto3
from botocore import UNSIGNED
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

# Refresh this many seconds before the token really expires.
EXPIRY_MARGIN = 60


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
        # Public app client calls are unsigned, no AWS credentials are needed.
        self._client = boto3.client(
            "cognito-idp",
            region_name=region,
            config=BotoConfig(signature_version=UNSIGNED),
        )
        self._tokens: Tokens | None = None
        self._lock = threading.Lock()

    @property
    def signed_in(self) -> bool:
        return self._tokens is not None

    def sign_in(self, username: str, password: str) -> None:
        try:
            resp = self._client.initiate_auth(
                ClientId=self._client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": username, "PASSWORD": password},
            )
        except (ClientError, BotoCoreError) as exc:
            raise AuthError(_message(exc)) from exc

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
                resp = self._client.initiate_auth(
                    ClientId=self._client_id,
                    AuthFlow="REFRESH_TOKEN_AUTH",
                    AuthParameters={"REFRESH_TOKEN": refresh_token},
                )
            except ClientError as exc:
                # Refresh token rejected (expired/revoked): session is over.
                self._tokens = None
                raise AuthError(_message(exc)) from exc
            except BotoCoreError as exc:
                # Connection problem: keep the tokens and let the caller retry.
                raise AuthUnavailable(_message(exc)) from exc
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


def _message(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Message") or str(exc)
    return str(exc)

"""Authentication for the Realtime Trains API.

The API takes a bearer token. You're issued either a long-life **access token**, used
directly, or a long-life **refresh token**, which you exchange at
`/api/get_access_token` for a short-life access token.
[`RefreshTokenAuth`][realtime_trains.auth.RefreshTokenAuth] does that exchange for you.
"""

from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import ValidationError

import nanosdk

from ._generated.models import AccessTokenResponse as AccessToken

__all__ = ["AccessToken", "RefreshTokenAuth"]


class RefreshTokenAuth(httpx.Auth):
    """Exchange a refresh token for short-life access tokens, renewing them as needed.

    The first request fetches an access token from `token_url` (sending the refresh
    token as the bearer), which is then reused until `leeway` before its `validUntil`.
    If the API answers 401 anyway, a new token is fetched and the request retried once.

    Example:
        ```python
        api = RealtimeTrains(refresh_token=os.environ["RTT_REFRESH_TOKEN"])
        ```

    Args:
        refresh_token: Your long-life refresh token.
        token_url: The token endpoint; absolute, since it doesn't go through the
            client's `base_url`.
        leeway: Renew this long before the token's `validUntil`.
        now: Current time as an aware datetime; replace it in tests.

    Attributes:
        token: The current access token response (with `entitlements` and
            `valid_until`), or `None` before the first request.
    """

    requires_response_body = True

    def __init__(
        self,
        refresh_token: str,
        *,
        token_url: str = "https://data.rtt.io/api/get_access_token",
        leeway: timedelta = timedelta(minutes=1),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._refresh_token = refresh_token
        self.token_url = token_url
        self.leeway = leeway
        self._now = now
        self.token: AccessToken | None = None

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        """Attach a valid access token, fetching or renewing it first when needed.

        Raises:
            nanosdk.APIStatusError: If the token endpoint rejects the refresh token.
        """
        if self._expired():
            self._store((yield self._token_request()))
        request.headers["Authorization"] = f"Bearer {self._access_token()}"
        response = yield request
        if response.status_code == 401:
            self._store((yield self._token_request()))
            request.headers["Authorization"] = f"Bearer {self._access_token()}"
            yield request

    def _expired(self) -> bool:
        if self.token is None or self.token.token is None:
            return True
        valid_until = self.token.valid_until
        if valid_until is None:
            return False  # no expiry given: keep it until a 401
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=UTC)
        return self._now() >= valid_until - self.leeway

    def _token_request(self) -> httpx.Request:
        headers = {"Authorization": f"Bearer {self._refresh_token}", "Accept": "application/json"}
        return httpx.Request("GET", self.token_url, headers=headers)

    def _store(self, response: httpx.Response) -> None:
        body: object = response.text
        if not response.is_error:
            try:
                self.token = AccessToken.model_validate_json(response.content)
            except ValidationError:
                pass
            else:
                if self.token.token:
                    return
        raise nanosdk.APIStatusError(response, body=body)

    def _access_token(self) -> str:
        assert self.token is not None
        assert self.token.token is not None
        return self.token.token

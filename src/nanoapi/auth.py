"""Ready-made `httpx.Auth` implementations. Pass one as `auth=` to any client.

```python
from nanoapi.auth import Bearer

api = Api("https://api.example.com", auth=Bearer(token))
```

They work with both [`Client`][nanoapi.Client] and [`AsyncClient`][nanoapi.AsyncClient]:
httpx drives the same flow either way. Any other `httpx.Auth` (or a `(user, password)`
tuple for basic auth) works as `auth=` too.
"""

import base64
import math
import time
from collections.abc import Callable, Generator, Mapping
from typing import Any, Literal, cast

import httpx

from .errors import APIStatusError

__all__ = ["ApiKey", "Bearer", "OAuth2ClientCredentials"]


class Bearer(httpx.Auth):
    """Send `Authorization: Bearer <token>` with every request.

    Example:
        ```python
        Bearer("s3cr3t")
        Bearer(lambda: vault.read("api-token"))  # read fresh for every request
        ```

    Args:
        token: The token, or a function returning it that's called for each request
            (for rotating credentials).
    """

    def __init__(self, token: str | Callable[[], str]) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        """Add the header (called by httpx for each request)."""
        token = self._token if isinstance(self._token, str) else self._token()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class ApiKey(httpx.Auth):
    """Send a static API key as a header or a query parameter.

    Example:
        ```python
        ApiKey("X-Api-Key", key)                    # X-Api-Key: <key>
        ApiKey("api_key", key, location="query")    # ?api_key=<key>
        ```

    Args:
        name: The header or query-parameter name.
        value: The key.
        location: Send it as a `"header"` or a `"query"` parameter.
    """

    def __init__(
        self, name: str, value: str, *, location: Literal["header", "query"] = "header"
    ) -> None:
        self.name = name
        self._value = value
        self.location = location

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        """Add the key (called by httpx for each request)."""
        if self.location == "header":
            request.headers[self.name] = self._value
        else:
            request.url = request.url.copy_merge_params({self.name: self._value})
        yield request


class OAuth2ClientCredentials(httpx.Auth):
    """OAuth 2 client-credentials grant (RFC 6749 §4.4), for machine-to-machine APIs.

    Fetches a token on first use and caches it until `leeway` seconds before it
    expires. If the API answers 401 anyway (a revoked token, say), it fetches a new
    token and retries the request once. A failed token request raises
    [`APIStatusError`][nanoapi.APIStatusError].

    Example:
        ```python
        auth = OAuth2ClientCredentials(
            "https://auth.example.com/oauth/token", client_id, client_secret, scope="read"
        )
        api = Api("https://api.example.com", auth=auth)
        ```

    One instance can be shared by several clients; concurrent requests that find the
    token expired may each fetch a new one.

    Args:
        token_url: Absolute URL of the token endpoint. Token requests don't go through
            the client's `base_url`.
        client_id: The client ID.
        client_secret: The client secret.
        scope: Space-separated scopes to request.
        extra: Extra form fields for the token request (for example `audience`).
        credentials_in: Send the credentials as HTTP basic auth (`"header"`, the
            RFC's recommendation) or as form fields (`"body"`).
        leeway: Refresh this many seconds before the token's `expires_in` runs out.
        clock: Monotonic clock in seconds; replace it in tests.

    Raises:
        ValueError: If `token_url` isn't absolute.
    """

    requires_response_body = True

    def __init__(
        self,
        token_url: str | httpx.URL,
        client_id: str,
        client_secret: str,
        *,
        scope: str | None = None,
        extra: Mapping[str, str] | None = None,
        credentials_in: Literal["header", "body"] = "header",
        leeway: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.token_url = httpx.URL(token_url)
        if not self.token_url.is_absolute_url:
            raise ValueError(f"token_url must be absolute, got {str(token_url)!r}")
        self.client_id = client_id
        self._client_secret = client_secret
        self.scope = scope
        self.extra = dict(extra or {})
        self.credentials_in = credentials_in
        self.leeway = leeway
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        """Fetch or reuse a token, send the request, and refresh once on a 401.

        Raises:
            APIStatusError: If the token endpoint rejects the credentials or answers
                without an `access_token`.
        """
        if self._token is None or self._clock() >= self._expires_at:
            self._store((yield self._token_request()))
        request.headers["Authorization"] = f"Bearer {self._token}"
        response = yield request
        if response.status_code == 401:
            self._store((yield self._token_request()))
            request.headers["Authorization"] = f"Bearer {self._token}"
            yield request

    def _token_request(self) -> httpx.Request:
        data = {"grant_type": "client_credentials", **self.extra}
        if self.scope:
            data["scope"] = self.scope
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.credentials_in == "header":
            raw = f"{self.client_id}:{self._client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        else:
            data |= {"client_id": self.client_id, "client_secret": self._client_secret}
        return httpx.Request("POST", self.token_url, data=data, headers=headers)

    def _store(self, response: httpx.Response) -> None:
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        if response.is_error or not isinstance(body, dict) or "access_token" not in body:
            raise APIStatusError(response, body=body)
        payload = cast(dict[str, Any], body)
        self._token = str(payload["access_token"])
        expires_in = payload.get("expires_in")
        self._expires_at = (
            self._clock() + float(expires_in) - self.leeway if expires_in else math.inf
        )

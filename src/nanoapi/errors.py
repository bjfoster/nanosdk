"""Exceptions raised by nanoapi clients.

Everything nanoapi raises (apart from pydantic's `ValidationError` for invalid call
arguments, which happens before a request is sent) derives from `NanoAPIError`:

```text
NanoAPIError
├── APIConnectionError        no response at all
│   └── APITimeoutError
├── APIStatusError            a 4xx/5xx response
└── ResponseValidationError   a 2xx response that doesn't match the declared type
```
"""

from typing import Any

import httpx
from pydantic import ValidationError

__all__ = [
    "APIConnectionError",
    "APIStatusError",
    "APITimeoutError",
    "NanoAPIError",
    "ResponseValidationError",
]


class NanoAPIError(Exception):
    """Base class for every error nanoapi raises.

    Catch it to handle any API failure in one place.
    """


class APIConnectionError(NanoAPIError):
    """The request never got a response (DNS failure, refused connection, TLS, ...).

    Raised after any retries allowed by the client's [`Retry`][nanoapi.Retry] policy
    are used up. The underlying `httpx.TransportError` is chained as `__cause__`.

    Args:
        message: Description of the failure.
        request: The request that couldn't be sent.

    Attributes:
        request: The request that couldn't be sent.
    """

    def __init__(self, message: str, *, request: httpx.Request) -> None:
        super().__init__(message)
        self.request = request


class APITimeoutError(APIConnectionError):
    """The request timed out (connect, read, write or pool timeout)."""


class APIStatusError(NanoAPIError):
    """The server answered with a 4xx or 5xx status.

    Raised by [`Client.raise_for_status`][nanoapi.Client.raise_for_status]; override that
    to raise your own exceptions instead.

    Example:
        ```python
        try:
            api.get_user(user_id=1)
        except nanoapi.APIStatusError as exc:
            if exc.status_code == 404:
                ...
            print(exc.error)  # parsed with error_model, if the client has one
        ```

    Args:
        response: The error response.
        body: The decoded response body.
        error: `body` validated against the client's `error_model`, or `None`.

    Attributes:
        response: The error response.
        request: The request that produced it.
        status_code: The HTTP status code.
        body: The response body as decoded JSON, or text if it wasn't JSON.
        error: `body` validated against the client's `error_model`; `None` if the client
            has no `error_model` or the body didn't match it.
    """

    def __init__(self, response: httpx.Response, *, body: Any, error: Any = None) -> None:
        self.response = response
        self.request = response.request
        self.status_code = response.status_code
        self.body = body
        self.error = error
        super().__init__(
            f"{response.request.method} {response.request.url} -> "
            f"{response.status_code} {response.reason_phrase}"
        )


class ResponseValidationError(NanoAPIError):
    """A successful response's body didn't match the route's declared response type.

    Args:
        response: The response whose body failed validation.
        error: The pydantic validation error.

    Attributes:
        response: The response whose body failed validation.
        error: The pydantic validation error, with details of every mismatch.
    """

    def __init__(self, response: httpx.Response, error: ValidationError) -> None:
        self.response = response
        self.error = error
        super().__init__(
            f"{response.request.method} {response.request.url}: response did not match "
            f"the declared type\n{error}"
        )

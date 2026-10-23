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
    """Base class for every error nanoapi raises."""


class APIConnectionError(NanoAPIError):
    """The request never got a response (DNS, refused connection, TLS, ...)."""

    def __init__(self, message: str, *, request: httpx.Request) -> None:
        super().__init__(message)
        self.request = request


class APITimeoutError(APIConnectionError):
    """The request timed out."""


class APIStatusError(NanoAPIError):
    """The server answered with a 4xx / 5xx status.

    ``body`` is the decoded JSON body (or text if it wasn't JSON).
    ``error`` is ``body`` validated against the client's ``error_model``, if one is set.
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
    """A 2xx response body didn't match the route's response type."""

    def __init__(self, response: httpx.Response, error: ValidationError) -> None:
        self.response = response
        self.error = error
        super().__init__(
            f"{response.request.method} {response.request.url}: response did not match "
            f"the declared type\n{error}"
        )

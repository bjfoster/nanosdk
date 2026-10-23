"""Exceptions for Realtime Trains API errors.

Each is an [`nanoapi.APIStatusError`][nanoapi.APIStatusError], so catching that (or
`nanoapi.NanoAPIError`) still catches everything.
"""

import nanoapi

__all__ = ["InvalidQuery", "NotEntitled", "NotFound", "Unauthorized"]


class InvalidQuery(nanoapi.APIStatusError):
    """400: the API rejected the query parameters."""


class Unauthorized(nanoapi.APIStatusError):
    """401: the token is missing, invalid or expired."""


class NotEntitled(nanoapi.APIStatusError):
    """403: your token's entitlements don't cover this request (see `api.info()`)."""


class NotFound(nanoapi.APIStatusError):
    """404: no such service (or location)."""


BY_STATUS: dict[int, type[nanoapi.APIStatusError]] = {
    400: InvalidQuery,
    401: Unauthorized,
    403: NotEntitled,
    404: NotFound,
}
"""Status code to exception class; anything else raises `nanoapi.APIStatusError`."""

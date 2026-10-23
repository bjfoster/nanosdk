"""Exceptions for Realtime Trains API errors.

Each is an [`nanosdk.APIStatusError`][nanosdk.APIStatusError], so catching that (or
`nanosdk.NanoSDKError`) still catches everything.
"""

import nanosdk

__all__ = ["InvalidQuery", "NotEntitled", "NotFound", "Unauthorized"]


class InvalidQuery(nanosdk.APIStatusError):
    """400: the API rejected the query parameters."""


class Unauthorized(nanosdk.APIStatusError):
    """401: the token is missing, invalid or expired."""


class NotEntitled(nanosdk.APIStatusError):
    """403: your token's entitlements don't cover this request (see `api.info()`)."""


class NotFound(nanosdk.APIStatusError):
    """404: no such service (or location)."""


BY_STATUS: dict[int, type[nanosdk.APIStatusError]] = {
    400: InvalidQuery,
    401: Unauthorized,
    403: NotEntitled,
    404: NotFound,
}
"""Status code to exception class; anything else raises `nanosdk.APIStatusError`."""

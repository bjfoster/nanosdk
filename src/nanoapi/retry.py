"""Retry policy for transient failures."""

import random
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

__all__ = ["Retry"]


@dataclass(frozen=True, slots=True)
class Retry:
    """When to re-send a request, and how long to wait first.

    Pass one as `retry=` to a client (or [`with_options`][nanoapi.Client.with_options]).
    Each attempt goes through the client's `prepare_request` hook again, so signatures
    and timestamps are fresh. Only idempotent `methods` are retried by default: add
    `"POST"` if your API deduplicates with idempotency keys.

    Delays grow exponentially (`backoff * 2**attempt`, capped at `max_delay`) with
    random jitter, unless the response carries a `Retry-After` header.

    Example:
        ```python
        api = Api(url, retry=nanoapi.Retry(max_retries=5, statuses={429, 503}))
        ```

    Subclass it and override [`should_retry`][nanoapi.Retry.should_retry] or
    [`delay`][nanoapi.Retry.delay] for custom rules; the client's
    [`retry_policy`][nanoapi.Client.retry_policy] hook picks a policy per route.

    Args:
        max_retries: Retries after the first attempt (so up to `max_retries + 1` tries).
        statuses: Response status codes that are retried.
        methods: HTTP methods that are retried.
        connection_errors: Whether to retry when no response arrived at all.
        backoff: Delay before the first retry, in seconds; doubles each attempt.
        max_delay: Upper bound for any single delay, in seconds (`Retry-After` included).
        jitter: Randomise each delay by up to plus or minus this fraction.
        respect_retry_after: Use the response's `Retry-After` header (seconds or HTTP
            date) as the delay when present.
    """

    max_retries: int = 2
    statuses: Collection[int] = frozenset({408, 429, 502, 503, 504})
    methods: Collection[str] = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
    connection_errors: bool = True
    backoff: float = 0.5  # first delay; doubles each attempt
    max_delay: float = 30.0
    jitter: float = 0.25  # +/- this fraction of the computed delay
    respect_retry_after: bool = True

    def should_retry(
        self, attempt: int, request: httpx.Request, response: httpx.Response | None
    ) -> bool:
        """Decide whether to send the request again.

        Args:
            attempt: Retries made so far (0 after the first attempt).
            request: The request that was sent.
            response: The response, or `None` if the attempt failed with a transport
                error (connection refused, timeout, ...).

        Returns:
            `True` to retry after [`delay`][nanoapi.Retry.delay] seconds.
        """
        if attempt >= self.max_retries or request.method not in self.methods:
            return False
        if response is None:
            return self.connection_errors
        return response.status_code in self.statuses

    def delay(self, attempt: int, response: httpx.Response | None) -> float:
        """Seconds to wait before the next attempt.

        Args:
            attempt: Retries made so far (0 after the first attempt).
            response: The response that triggered the retry, or `None` after a
                transport error.

        Returns:
            The `Retry-After` delay if present and respected, otherwise exponential
            backoff with jitter; never more than `max_delay`.
        """
        if response is not None and self.respect_retry_after:
            after = _retry_after(response)
            if after is not None:
                return min(after, self.max_delay)
        base = min(self.backoff * 2.0**attempt, self.max_delay)
        return base * random.uniform(1 - self.jitter, 1 + self.jitter)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)

import random
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

__all__ = ["Retry"]


@dataclass(frozen=True, slots=True)
class Retry:
    """When and how long to wait before re-sending a request.

    Pass as ``retry=`` to a client. Only idempotent ``methods`` are retried; add ``"POST"``
    if your API takes idempotency keys. Subclass and override ``should_retry`` / ``delay``
    for anything fancier.
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
        """``response`` is ``None`` when the attempt failed with a transport error."""
        if attempt >= self.max_retries or request.method not in self.methods:
            return False
        if response is None:
            return self.connection_errors
        return response.status_code in self.statuses

    def delay(self, attempt: int, response: httpx.Response | None) -> float:
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

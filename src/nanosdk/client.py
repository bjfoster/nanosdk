"""The clients, and the bound routes you get when you access a route on one.

```python
class GitHub(nanosdk.Client):
    get_user = nanosdk.get("/users/{username}", request=GetUser, response=User)

with GitHub("https://api.github.com") as gh:
    user = gh.get_user(username="octocat")      # gh.get_user is a BoundRoute
```

A client's public attributes are your routes plus `with_options`, `close` / `aclose`
and the context-manager protocol. Everything else is underscored, including the hooks
you override (`_prepare_request`, `_process_response`, `_raise_for_status`,
`_retry_policy`), so routes can be named anything except those few public names.
"""

# Bound routes call their client's underscored internals: the two are one unit.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import copy
import itertools
import logging
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from types import TracebackType
from typing import Any, Self, overload

import httpx
from pydantic import TypeAdapter, ValidationError

from .errors import APIConnectionError, APIStatusError, APITimeoutError
from .pagination import Page
from .retry import Retry
from .routing import PaginatedRoute, Route, Routes

__all__ = [
    "AsyncBoundPaginatedRoute",
    "AsyncBoundRoute",
    "AsyncClient",
    "BoundPaginatedRoute",
    "BoundRoute",
    "Client",
    "http_client",
]

type TimeoutTypes = float | httpx.Timeout | None
"""A timeout in seconds, an `httpx.Timeout`, or `None` for no timeout."""

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
"""30 seconds per read/write/pool wait, 10 to connect."""

LOGGER = logging.getLogger("nanosdk")
"""The default logger for clients; pass `logger=` to a client to use another."""


class _NotGiven:
    def __repr__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN = _NotGiven()


class _BaseClient[HttpT: (httpx.Client, httpx.AsyncClient)](Routes):
    """Shared plumbing for `Client` and `AsyncClient`."""

    _http: HttpT

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__module__ == __name__:
            return  # Client / AsyncClient themselves
        reserved = {name for base in (_BaseClient, Client, AsyncClient) for name in vars(base)}
        for klass in cls.__mro__:
            if klass.__module__ == __name__:
                continue
            for name, value in vars(klass).items():
                if name in reserved and isinstance(value, Route | PaginatedRoute):
                    raise TypeError(
                        f"{cls.__name__}: a route can't be called {name!r}, nanosdk uses "
                        f"that name on the client; pick another (e.g. {name + '_'!r})"
                    )

    def _setup(
        self,
        base_url: str | httpx.URL,
        headers: Mapping[str, str] | None,
        timeout: TimeoutTypes,
        auth: httpx.Auth | tuple[str, str] | None,
        error_model: type[Any] | None,
        retry: Retry | None,
        logger: logging.Logger | None,
        http_client: HttpT | None,
        httpx_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        self._logger = logger or LOGGER
        self._extra_headers: dict[str, str] = {}
        self._timeout: Any = httpx.USE_CLIENT_DEFAULT
        self._retry = retry
        self._error_adapter: TypeAdapter[Any] | None = (
            TypeAdapter(error_model) if error_model is not None else None
        )
        self._owns_http = http_client is None
        if http_client is not None and (base_url or headers or auth or httpx_kwargs):
            raise ValueError(
                "pass either http_client or base_url/headers/auth/httpx options, not both "
                "(configure the httpx client you pass in instead)"
            )
        return {
            "base_url": base_url,
            "headers": dict(headers or {}),
            "timeout": timeout,
            "auth": auth,
            **httpx_kwargs,
        }

    # -- configuration ------------------------------------------------------

    def with_options(
        self,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: TimeoutTypes | _NotGiven = NOT_GIVEN,
        retry: Retry | _NotGiven | None = NOT_GIVEN,
    ) -> Self:
        """Return a copy of the client with extra headers or a different timeout/retry.

        The copy is the same class, so all routes are available on it, and it shares
        the original's connection pool. It never closes the pool: close the original.

        Example:
            ```python
            scoped = api.with_options(headers={"Authorization": f"Bearer {token}"})
            slow = api.with_options(timeout=120, retry=None)
            ```

        Args:
            headers: Headers added to every request, on top of existing ones.
            timeout: Replaces the timeout; `None` disables it.
            retry: Replaces the retry policy; `None` disables retries.

        Returns:
            The configured copy.
        """
        clone = copy.copy(self)
        clone._extra_headers = {**self._extra_headers, **(headers or {})}
        if not isinstance(timeout, _NotGiven):
            clone._timeout = timeout
        if not isinstance(retry, _NotGiven):
            clone._retry = retry
        clone._owns_http = False
        return clone

    # -- overridable behaviour ----------------------------------------------

    def _retry_policy(self, route: Route[Any, Any, Any]) -> Retry | None:
        """Hook: choose the retry policy for a route. Override to vary it per route.

        Example:
            ```python
            def _retry_policy(self, route):
                if route.meta.get("no_retry"):
                    return None
                return super()._retry_policy(route)
            ```

        Args:
            route: The route about to be sent.

        Returns:
            The policy to use, or `None` for no retries. By default, the client's
            `retry=` setting.
        """
        return self._retry

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Hook: raise if `response` is an error. Override to map errors your own way.

        Called after `_process_response` and before decoding, for every call except
        `.raw()`. The default raises
        [`APIStatusError`][nanosdk.APIStatusError] on a 4xx or 5xx status, with the body
        parsed by the client's `error_model` when one is set.

        Args:
            response: The final response for a call.

        Raises:
            APIStatusError: If the status is 4xx or 5xx.
        """
        if not response.is_error:
            return
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        error: Any = None
        if self._error_adapter is not None:
            try:
                error = self._error_adapter.validate_python(body)
            except ValidationError:
                error = None
        raise APIStatusError(response, body=body, error=error)

    # -- request / response plumbing ----------------------------------------

    def _build_request[ReqT](
        self, route: Route[Any, ReqT, Any], request: ReqT, *, url: httpx.URL | None = None
    ) -> httpx.Request:
        """Build, but don't send, the request for a route and request model.

        Includes the client's headers and timeout. Auth and the `_prepare_request` hook
        are applied only when sending.

        Args:
            route: The route.
            request: An instance of the route's request model (`None` if it has none).
            url: Replaces the route's URL and query string; used to follow pagination
                links.

        Returns:
            The unsent request.
        """
        enc = route.encode(request)
        headers = {**self._extra_headers, **enc.headers}
        return self._http.build_request(
            enc.method,
            enc.url if url is None else url,
            params=(enc.params or None) if url is None else None,
            headers=headers or None,
            json=enc.json if enc.has_json else None,
            content=enc.content,
            data=enc.data,
            files=enc.files,
            timeout=self._timeout,
        )

    @staticmethod
    def _wrap_transport_error(
        exc: httpx.TransportError, request: httpx.Request
    ) -> APIConnectionError:
        msg = str(exc) or type(exc).__name__
        if isinstance(exc, httpx.TimeoutException):
            return APITimeoutError(msg, request=request)
        return APIConnectionError(msg, request=request)

    def _next_page[ReqT](
        self,
        route: PaginatedRoute[Any, ReqT, Any, Any],
        page: Page[ReqT, Any, Any],
        url: httpx.URL | None,
        number: int,
    ) -> tuple[ReqT, httpx.URL | None] | None:
        """Where the page after `page` comes from, or `None` when done (or stuck)."""
        name = route.route.name
        self._logger.debug(
            "Page %d of %s has %d item(s)", number, name, len(page.items),
            extra={"route": name, "page": number, "items": len(page.items)},
        )  # fmt: skip
        nxt = route.next_page(page)
        if nxt is None:
            return None
        if isinstance(nxt, httpx.URL):
            stuck = nxt == page.response.request.url
        else:
            stuck = nxt == page.request and url is None
        if stuck:
            self._logger.warning(
                "Stopped paginating %s after page %d: the strategy asked for the same page again",
                name, number, extra={"route": name, "page": number},
            )  # fmt: skip
            return None
        return (page.request, nxt) if isinstance(nxt, httpx.URL) else (nxt, None)

    # -- logging ----------------------------------------------------------------

    def _log_sent(
        self,
        route: Route[Any, Any, Any],
        target: tuple[str, str],
        attempt: int,
        started: float,
        outcome: httpx.Response | Exception,
    ) -> None:
        """Log one attempt's outcome at DEBUG: a response, or the transport error."""
        if not self._logger.isEnabledFor(logging.DEBUG):
            return
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        method, url = target
        fields = _fields(route, target, attempt, elapsed_ms=elapsed)
        if isinstance(outcome, httpx.Response):
            fields["status_code"] = outcome.status_code
            self._logger.debug(
                "%s %s -> %d %s in %.0f ms (%s, attempt %d)",
                method, url, outcome.status_code, outcome.reason_phrase,
                elapsed, fields["route"], attempt + 1, extra=fields,
            )  # fmt: skip
        else:
            self._logger.debug(
                "%s %s failed in %.0f ms: %s (%s, attempt %d)",
                method, url, elapsed, _describe(outcome), fields["route"],
                attempt + 1, extra=fields,
            )  # fmt: skip

    def _log_retry(
        self,
        route: Route[Any, Any, Any],
        target: tuple[str, str],
        attempt: int,
        delay: float,
        retry: Retry,
        outcome: httpx.Response | Exception,
    ) -> None:
        """Log at WARNING that an attempt failed and will be retried."""
        reason = (
            f"{outcome.status_code} {outcome.reason_phrase}"
            if isinstance(outcome, httpx.Response)
            else _describe(outcome)
        )
        method, url = target
        fields = _fields(route, target, attempt, retry_delay=round(delay, 3))
        if isinstance(outcome, httpx.Response):
            fields["status_code"] = outcome.status_code
        self._logger.warning(
            "Retrying %s %s in %.2fs after %s (%s, retry %d of %d)",
            method, url, delay, reason, fields["route"], attempt + 1,
            retry.max_retries, extra=fields,
        )  # fmt: skip

    def __repr__(self) -> str:
        return f"<{type(self).__name__} base_url={str(self._http.base_url)!r}>"


def _fields(
    route: Route[Any, Any, Any], target: tuple[str, str], attempt: int, **more: Any
) -> dict[str, Any]:
    """Structured `extra` fields for a log record about one attempt."""
    return {
        "route": route.name or route.path,
        "method": target[0],
        "url": target[1],
        "attempt": attempt + 1,
        **more,
    }


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


class Client(_BaseClient[httpx.Client]):
    """Synchronous API client, built on `httpx.Client`.

    Subclass it and declare routes as class attributes; on an instance each route
    becomes a [`BoundRoute`][nanosdk.BoundRoute] method:

    ```python
    class GitHub(nanosdk.Client):
        get_user = nanosdk.get("/users/{username}", request=GetUser, response=User)

    with GitHub("https://api.github.com") as gh:
        user = gh.get_user(username="octocat")  # -> User
    ```

    Apart from your routes, an instance only exposes
    [`with_options`][nanosdk.Client.with_options] and [`close`][nanosdk.Client.close] (or
    use it as a context manager). Customise behaviour by overriding the underscored
    hooks [`_prepare_request`][nanosdk.Client._prepare_request],
    [`_process_response`][nanosdk.Client._process_response],
    [`_raise_for_status`][nanosdk.Client._raise_for_status] and
    [`_retry_policy`][nanosdk.Client._retry_policy]. A route named like one of the
    client's own attributes (`close`, `with_options`, a hook, ...) raises `TypeError`
    when the class is defined.

    Args:
        base_url: Prefix for every route path, such as `"https://api.example.com/v2"`.
        headers: Headers sent with every request.
        timeout: Seconds, an `httpx.Timeout`, or `None` to wait forever.
        auth: Any `httpx.Auth` (see [`nanosdk.auth`][nanosdk.auth]) or a
            `(username, password)` tuple for basic auth.
        error_model: A type to parse 4xx/5xx bodies into, exposed as
            [`APIStatusError.error`][nanosdk.APIStatusError].
        retry: Retry policy for transient failures; `None` (the default) never retries.
        logger: Where to log requests, responses and retries; defaults to the `nanosdk`
            logger. SDKs can pass their own, such as `logging.getLogger("my_sdk")`.
        http_client: Use this `httpx.Client` instead of creating one. It isn't closed
            for you, and can't be combined with `base_url`, `headers`, `auth` or
            `httpx_kwargs`: configure it directly.
        **httpx_kwargs: Any other `httpx.Client` argument (`transport`, `proxy`,
            `verify`, `limits`, `event_hooks`, ...).

    Raises:
        ValueError: If `http_client` is combined with options that configure httpx.
    """

    def __init__(
        self,
        base_url: str | httpx.URL = "",
        *,
        headers: Mapping[str, str] | None = None,
        timeout: TimeoutTypes = DEFAULT_TIMEOUT,
        auth: httpx.Auth | tuple[str, str] | None = None,
        error_model: type[Any] | None = None,
        retry: Retry | None = None,
        logger: logging.Logger | None = None,
        http_client: httpx.Client | None = None,
        **httpx_kwargs: Any,
    ) -> None:
        opts = self._setup(
            base_url, headers, timeout, auth, error_model, retry, logger, http_client, httpx_kwargs
        )
        self._http = httpx.Client(**opts) if http_client is None else http_client

    def _bind_route[**P, ReqT, RespT](
        self, route: Route[P, ReqT, RespT]
    ) -> BoundRoute[P, ReqT, RespT]:
        return BoundRoute(route, self)

    def _bind_paginated[**P, ReqT, RespT, ItemT](
        self, route: PaginatedRoute[P, ReqT, RespT, ItemT]
    ) -> BoundPaginatedRoute[P, ReqT, RespT, ItemT]:
        return BoundPaginatedRoute(route, self)

    # -- hooks ----------------------------------------------------------------

    def _prepare_request(
        self, request: httpx.Request, route: Route[Any, Any, Any]
    ) -> httpx.Request:
        """Hook called on every outgoing request, including each retry. Override it.

        Use it to sign requests, add per-request headers or log. Auth from `auth=` is
        applied after this hook.

        Example:
            ```python
            def _prepare_request(self, request, route):
                if route.meta.get("signed", True):
                    request.headers["X-Signature"] = sign(request)
                return request
            ```

        Args:
            request: The request about to be sent.
            route: The route being called; read `route.name` or `route.meta`.

        Returns:
            The request to send: `request` itself (modified or not) or a replacement.
        """
        return request

    def _process_response(
        self, response: httpx.Response, route: Route[Any, Any, Any]
    ) -> httpx.Response:
        """Hook called on every final response, before the status check and decoding.

        Override it to unwrap response envelopes, record rate limits or log. Responses
        that are retried don't reach it.

        Args:
            response: The response.
            route: The route that was called.

        Returns:
            The response to check and decode: `response` or a replacement. Pass
            `request=response.request` when building a new `httpx.Response`.
        """
        return response

    # -- internals used by bound routes -------------------------------------------

    def _send[ReqT, RespT](self, route: Route[Any, ReqT, RespT], request: ReqT) -> RespT:
        response = self._request(route, request, None)
        self._raise_for_status(response)
        return route.parse(response)

    def _request[ReqT](
        self, route: Route[Any, ReqT, Any], request: ReqT, url: httpx.URL | None
    ) -> httpx.Response:
        """Send with hooks and retries; no status check, no decoding."""
        retry = self._retry_policy(route)
        attempt = 0
        while True:
            http_request = self._prepare_request(
                self._build_request(route, request, url=url), route
            )
            # Snapshot before sending: httpx auth mutates the request (adding query keys).
            target = (http_request.method, str(http_request.url))
            started = time.perf_counter()
            try:
                response = self._http.send(http_request)
            except httpx.TransportError as exc:
                self._log_sent(route, target, attempt, started, exc)
                if retry is None or not retry.should_retry(attempt, http_request, None):
                    raise self._wrap_transport_error(exc, http_request) from exc
                delay = retry.delay(attempt, None)
                self._log_retry(route, target, attempt, delay, retry, exc)
            else:
                self._log_sent(route, target, attempt, started, response)
                if retry is None or not retry.should_retry(attempt, http_request, response):
                    return self._process_response(response, route)
                response.close()
                delay = retry.delay(attempt, response)
                self._log_retry(route, target, attempt, delay, retry, response)
            time.sleep(delay)
            attempt += 1

    def _iter_pages[ReqT, RespT, ItemT](
        self, route: PaginatedRoute[Any, ReqT, RespT, ItemT], request: ReqT
    ) -> Iterator[Page[ReqT, RespT, ItemT]]:
        url: httpx.URL | None = None
        for number in itertools.count(1):
            response = self._request(route.route, request, url)
            self._raise_for_status(response)
            page = route.make_page(request, response)
            yield page
            step = self._next_page(route, page, url, number)
            if step is None:
                return
            request, url = step

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Close the connection pool, unless it came from `http_client=`."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class AsyncClient(_BaseClient[httpx.AsyncClient]):
    """Asynchronous API client, built on `httpx.AsyncClient`.

    The async twin of [`Client`][nanosdk.Client], with the same constructor arguments
    (`http_client` takes an `httpx.AsyncClient`). Route calls return awaitables,
    paginated routes give async iterators, the `_prepare_request` /
    `_process_response` hooks are `async def`, and `aclose` replaces `close`.

    ```python
    class AsyncGitHub(Users, Repos, nanosdk.AsyncClient): ...

    async with AsyncGitHub("https://api.github.com") as gh:
        user = await gh.get_user(username="octocat")
        async for repo in gh.list_repos.iter(username="octocat"): ...
    ```

    Args:
        base_url: Prefix for every route path.
        headers: Headers sent with every request.
        timeout: Seconds, an `httpx.Timeout`, or `None` to wait forever.
        auth: Any `httpx.Auth` or a `(username, password)` tuple.
        error_model: A type to parse 4xx/5xx bodies into.
        retry: Retry policy for transient failures; `None` never retries.
        logger: Where to log; defaults to the `nanosdk` logger.
        http_client: Use this `httpx.AsyncClient` instead of creating one.
        **httpx_kwargs: Any other `httpx.AsyncClient` argument.

    Raises:
        ValueError: If `http_client` is combined with options that configure httpx.
    """

    def __init__(
        self,
        base_url: str | httpx.URL = "",
        *,
        headers: Mapping[str, str] | None = None,
        timeout: TimeoutTypes = DEFAULT_TIMEOUT,
        auth: httpx.Auth | tuple[str, str] | None = None,
        error_model: type[Any] | None = None,
        retry: Retry | None = None,
        logger: logging.Logger | None = None,
        http_client: httpx.AsyncClient | None = None,
        **httpx_kwargs: Any,
    ) -> None:
        opts = self._setup(
            base_url, headers, timeout, auth, error_model, retry, logger, http_client, httpx_kwargs
        )
        self._http = httpx.AsyncClient(**opts) if http_client is None else http_client

    def _bind_route[**P, ReqT, RespT](
        self, route: Route[P, ReqT, RespT]
    ) -> AsyncBoundRoute[P, ReqT, RespT]:
        return AsyncBoundRoute(route, self)

    def _bind_paginated[**P, ReqT, RespT, ItemT](
        self, route: PaginatedRoute[P, ReqT, RespT, ItemT]
    ) -> AsyncBoundPaginatedRoute[P, ReqT, RespT, ItemT]:
        return AsyncBoundPaginatedRoute(route, self)

    # -- hooks ----------------------------------------------------------------

    async def _prepare_request(
        self, request: httpx.Request, route: Route[Any, Any, Any]
    ) -> httpx.Request:
        """Hook called on every outgoing request, including each retry.

        The async version of [`Client._prepare_request`][nanosdk.Client._prepare_request].

        Args:
            request: The request about to be sent.
            route: The route being called.

        Returns:
            The request to send.
        """
        return request

    async def _process_response(
        self, response: httpx.Response, route: Route[Any, Any, Any]
    ) -> httpx.Response:
        """Hook called on every final response, before the status check and decoding.

        The async version of [`Client._process_response`][nanosdk.Client._process_response].

        Args:
            response: The response.
            route: The route that was called.

        Returns:
            The response to check and decode.
        """
        return response

    # -- internals used by bound routes -------------------------------------------

    async def _send[ReqT, RespT](self, route: Route[Any, ReqT, RespT], request: ReqT) -> RespT:
        response = await self._request(route, request, None)
        self._raise_for_status(response)
        return route.parse(response)

    async def _request[ReqT](
        self, route: Route[Any, ReqT, Any], request: ReqT, url: httpx.URL | None
    ) -> httpx.Response:
        """Send with hooks and retries; no status check, no decoding."""
        retry = self._retry_policy(route)
        attempt = 0
        while True:
            http_request = await self._prepare_request(
                self._build_request(route, request, url=url), route
            )
            target = (http_request.method, str(http_request.url))  # before auth mutates it
            started = time.perf_counter()
            try:
                response = await self._http.send(http_request)
            except httpx.TransportError as exc:
                self._log_sent(route, target, attempt, started, exc)
                if retry is None or not retry.should_retry(attempt, http_request, None):
                    raise self._wrap_transport_error(exc, http_request) from exc
                delay = retry.delay(attempt, None)
                self._log_retry(route, target, attempt, delay, retry, exc)
            else:
                self._log_sent(route, target, attempt, started, response)
                if retry is None or not retry.should_retry(attempt, http_request, response):
                    return await self._process_response(response, route)
                await response.aclose()
                delay = retry.delay(attempt, response)
                self._log_retry(route, target, attempt, delay, retry, response)
            await asyncio.sleep(delay)
            attempt += 1

    async def _iter_pages[ReqT, RespT, ItemT](
        self, route: PaginatedRoute[Any, ReqT, RespT, ItemT], request: ReqT
    ) -> AsyncIterator[Page[ReqT, RespT, ItemT]]:
        url: httpx.URL | None = None
        for number in itertools.count(1):
            response = await self._request(route.route, request, url)
            self._raise_for_status(response)
            page = route.make_page(request, response)
            yield page
            step = self._next_page(route, page, url, number)
            if step is None:
                return
            request, url = step

    @staticmethod
    async def _items[ItemT](pages: AsyncIterator[Page[Any, Any, ItemT]]) -> AsyncIterator[ItemT]:
        async for page in pages:
            for item in page.items:
                yield item

    # -- lifecycle ------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the connection pool, unless it came from `http_client=`."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


class BoundRoute[**P, ReqT, RespT]:
    """A route attached to a [`Client`][nanosdk.Client]; what `api.some_route` gives you.

    Call it with the request model's arguments to send the request and get the decoded
    response. The other methods are variations for when you need more control.

    ```python
    api.get_user(user_id=1)                 # -> User
    api.get_user.send(GetUser(user_id=1))   # already-built model
    api.get_user.raw(user_id=1)             # httpx.Response, no status check / decoding
    api.get_user.build_request(user_id=1)   # httpx.Request, not sent
    ```

    Attributes:
        route: The underlying [`Route`][nanosdk.Route].
        client: The client it's bound to.
    """

    __slots__ = ("client", "route")

    def __init__(self, route: Route[P, ReqT, RespT], client: Client) -> None:
        self.route = route
        self.client = client

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> RespT:
        """Build the request model from the arguments, send it, and decode the response.

        Returns:
            The decoded response.

        Raises:
            pydantic.ValidationError: If the arguments are invalid; nothing is sent.
            APIStatusError: On a 4xx/5xx response.
            APIConnectionError: If no response arrived, after any retries.
            ResponseValidationError: If the body doesn't match the response type.
        """
        return self.client._send(self.route, self.route.build_model(*args, **kwargs))

    def send(self, request: ReqT) -> RespT:
        """Send an already-built request model and decode the response.

        Args:
            request: An instance of the route's request model.

        Returns:
            The decoded response.
        """
        return self.client._send(self.route, request)

    def raw(self, *args: P.args, **kwargs: P.kwargs) -> httpx.Response:
        """Like calling the route, but return the `httpx.Response` as-is.

        Hooks and retries apply; there's no status check and no decoding.

        Returns:
            The response, whatever its status.
        """
        return self.client._request(self.route, self.route.build_model(*args, **kwargs), None)

    def build_request(self, *args: P.args, **kwargs: P.kwargs) -> httpx.Request:
        """Build the request that calling the route would send, without sending it.

        Auth and the `_prepare_request` hook aren't applied; they run at send time.

        Returns:
            The unsent request.
        """
        return self.client._build_request(self.route, self.route.build_model(*args, **kwargs))

    def __repr__(self) -> str:
        return f"<bound {self.route!r}>"


class BoundPaginatedRoute[**P, ReqT, RespT, ItemT](BoundRoute[P, ReqT, RespT]):
    """A paginated route attached to a [`Client`][nanosdk.Client].

    Calling it fetches just the first page, like any [`BoundRoute`][nanosdk.BoundRoute];
    `iter` and `pages` walk all of them.

    ```python
    first = api.list_posts(limit=50)            # the first page's response
    for post in api.list_posts.iter(limit=50):  # every post, page by page
        ...
    ```

    Attributes:
        paginated: The underlying [`PaginatedRoute`][nanosdk.PaginatedRoute].
    """

    __slots__ = ("paginated",)

    def __init__(self, paginated: PaginatedRoute[P, ReqT, RespT, ItemT], client: Client) -> None:
        super().__init__(paginated.route, client)
        self.paginated = paginated

    def iter(self, *args: P.args, **kwargs: P.kwargs) -> Iterator[ItemT]:
        """Iterate over every item on every page.

        Arguments are validated immediately; pages are fetched as you iterate, so
        `itertools.islice` or `break` stops further requests.

        Returns:
            A lazy iterator over the items.
        """
        return (item for page in self.pages(*args, **kwargs) for item in page.items)

    def pages(self, *args: P.args, **kwargs: P.kwargs) -> Iterator[Page[ReqT, RespT, ItemT]]:
        """Iterate over every page, with each page's request, data, items and response.

        Arguments are validated immediately; pages are fetched as you iterate.

        Returns:
            A lazy iterator over [`Page`][nanosdk.Page] objects.
        """
        return self.client._iter_pages(self.paginated, self.route.build_model(*args, **kwargs))


class AsyncBoundRoute[**P, ReqT, RespT]:
    """A route attached to an [`AsyncClient`][nanosdk.AsyncClient].

    The async twin of [`BoundRoute`][nanosdk.BoundRoute]: calling it, `send` and `raw`
    return awaitables.

    Attributes:
        route: The underlying [`Route`][nanosdk.Route].
        client: The client it's bound to.
    """

    __slots__ = ("client", "route")

    def __init__(self, route: Route[P, ReqT, RespT], client: AsyncClient) -> None:
        self.route = route
        self.client = client

    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> RespT:
        """Build the request model, send it, and decode the response.

        Returns:
            The decoded response.
        """
        return await self.client._send(self.route, self.route.build_model(*args, **kwargs))

    async def send(self, request: ReqT) -> RespT:
        """Send an already-built request model and decode the response.

        Args:
            request: An instance of the route's request model.

        Returns:
            The decoded response.
        """
        return await self.client._send(self.route, request)

    async def raw(self, *args: P.args, **kwargs: P.kwargs) -> httpx.Response:
        """Like calling the route, but return the `httpx.Response` as-is.

        Returns:
            The response, whatever its status.
        """
        model = self.route.build_model(*args, **kwargs)
        return await self.client._request(self.route, model, None)

    def build_request(self, *args: P.args, **kwargs: P.kwargs) -> httpx.Request:
        """Build the request that calling the route would send, without sending it.

        Returns:
            The unsent request.
        """
        return self.client._build_request(self.route, self.route.build_model(*args, **kwargs))

    def __repr__(self) -> str:
        return f"<bound async {self.route!r}>"


class AsyncBoundPaginatedRoute[**P, ReqT, RespT, ItemT](AsyncBoundRoute[P, ReqT, RespT]):
    """A paginated route attached to an [`AsyncClient`][nanosdk.AsyncClient].

    ```python
    async for post in api.list_posts.iter(limit=50):
        ...
    ```

    Attributes:
        paginated: The underlying [`PaginatedRoute`][nanosdk.PaginatedRoute].
    """

    __slots__ = ("paginated",)

    def __init__(
        self, paginated: PaginatedRoute[P, ReqT, RespT, ItemT], client: AsyncClient
    ) -> None:
        super().__init__(paginated.route, client)
        self.paginated = paginated

    def iter(self, *args: P.args, **kwargs: P.kwargs) -> AsyncIterator[ItemT]:
        """Iterate over every item on every page with `async for`.

        Returns:
            A lazy async iterator over the items.
        """
        return self.client._items(self.pages(*args, **kwargs))

    def pages(self, *args: P.args, **kwargs: P.kwargs) -> AsyncIterator[Page[ReqT, RespT, ItemT]]:
        """Iterate over every page with `async for`.

        Returns:
            A lazy async iterator over [`Page`][nanosdk.Page] objects.
        """
        return self.client._iter_pages(self.paginated, self.route.build_model(*args, **kwargs))


@overload
def http_client(client: Client) -> httpx.Client: ...
@overload
def http_client(client: AsyncClient) -> httpx.AsyncClient: ...
def http_client(client: Client | AsyncClient) -> httpx.Client | httpx.AsyncClient:
    """The httpx client underneath a nanosdk client, for anything nanosdk doesn't wrap.

    A function rather than an attribute, so it can't collide with a route name.

    Example:
        ```python
        nanosdk.http_client(api).cookies.set("session", "...")
        ```

    Args:
        client: A nanosdk client.

    Returns:
        Its `httpx.Client` (or `httpx.AsyncClient`).
    """
    return client._http

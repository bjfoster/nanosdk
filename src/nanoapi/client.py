"""The clients, and the bound routes you get when you access a route on one.

```python
class GitHub(nanoapi.Client):
    get_user = nanoapi.get("/users/{username}", request=GetUser, response=User)

with GitHub("https://api.github.com") as gh:
    user = gh.get_user(username="octocat")      # gh.get_user is a BoundRoute
```
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from types import TracebackType
from typing import Any, Self

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
]

type TimeoutTypes = float | httpx.Timeout | None
"""A timeout in seconds, an `httpx.Timeout`, or `None` for no timeout."""

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
"""30 seconds per read/write/pool wait, 10 to connect."""


class _NotGiven:
    def __repr__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN = _NotGiven()


class _BaseClient[HttpT: (httpx.Client, httpx.AsyncClient)](Routes):
    """Shared plumbing for `Client` and `AsyncClient`."""

    _http: HttpT

    def _setup(
        self,
        base_url: str | httpx.URL,
        headers: Mapping[str, str] | None,
        timeout: TimeoutTypes,
        auth: httpx.Auth | tuple[str, str] | None,
        error_model: type[Any] | None,
        retry: Retry | None,
        http_client: HttpT | None,
        httpx_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
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

    @property
    def http(self) -> HttpT:
        """The underlying httpx client, for anything nanoapi doesn't wrap."""
        return self._http

    # -- overridable behaviour ----------------------------------------------

    def retry_policy(self, route: Route[Any, Any, Any]) -> Retry | None:
        """Choose the retry policy for a route. Override to vary it per route.

        Example:
            ```python
            def retry_policy(self, route):
                if route.meta.get("no_retry"):
                    return None
                return super().retry_policy(route)
            ```

        Args:
            route: The route about to be sent.

        Returns:
            The policy to use, or `None` for no retries. By default, the client's
            `retry=` setting.
        """
        return self._retry

    def raise_for_status(self, response: httpx.Response) -> None:
        """Raise if `response` is an error. Override to map errors your own way.

        Called after `process_response` and before decoding, for every call except
        `send_raw` / `.raw()`. The default raises
        [`APIStatusError`][nanoapi.APIStatusError] on a 4xx or 5xx status, with the body
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

    def build_request[ReqT](
        self, route: Route[Any, ReqT, Any], request: ReqT, *, url: httpx.URL | None = None
    ) -> httpx.Request:
        """Build, but don't send, the request for a route and request model.

        Includes the client's headers and timeout. Auth and the `prepare_request` hook
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

    @staticmethod
    def _next_page[ReqT](
        route: PaginatedRoute[Any, ReqT, Any, Any],
        page: Page[ReqT, Any, Any],
        url: httpx.URL | None,
    ) -> tuple[ReqT, httpx.URL | None] | None:
        """Where the page after `page` comes from, or `None` when done (or stuck)."""
        nxt = route.next_page(page)
        if nxt is None:
            return None
        if isinstance(nxt, httpx.URL):
            return None if nxt == page.response.request.url else (page.request, nxt)
        return None if (nxt == page.request and url is None) else (nxt, None)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} base_url={str(self._http.base_url)!r}>"


class Client(_BaseClient[httpx.Client]):
    """Synchronous API client, built on `httpx.Client`.

    Subclass it and declare routes as class attributes; on an instance each route
    becomes a [`BoundRoute`][nanoapi.BoundRoute] method:

    ```python
    class GitHub(nanoapi.Client):
        get_user = nanoapi.get("/users/{username}", request=GetUser, response=User)

    with GitHub("https://api.github.com") as gh:
        user = gh.get_user(username="octocat")  # -> User
    ```

    Customise behaviour by overriding [`prepare_request`][nanoapi.Client.prepare_request],
    [`process_response`][nanoapi.Client.process_response],
    [`raise_for_status`][nanoapi.Client.raise_for_status] or
    [`retry_policy`][nanoapi.Client.retry_policy]. Use the client as a context manager,
    or call [`close`][nanoapi.Client.close], to release connections.

    Args:
        base_url: Prefix for every route path, such as `"https://api.example.com/v2"`.
        headers: Headers sent with every request.
        timeout: Seconds, an `httpx.Timeout`, or `None` to wait forever.
        auth: Any `httpx.Auth` (see [`nanoapi.auth`][nanoapi.auth]) or a
            `(username, password)` tuple for basic auth.
        error_model: A type to parse 4xx/5xx bodies into, exposed as
            [`APIStatusError.error`][nanoapi.APIStatusError].
        retry: Retry policy for transient failures; `None` (the default) never retries.
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
        http_client: httpx.Client | None = None,
        **httpx_kwargs: Any,
    ) -> None:
        opts = self._setup(
            base_url, headers, timeout, auth, error_model, retry, http_client, httpx_kwargs
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

    def prepare_request(self, request: httpx.Request, route: Route[Any, Any, Any]) -> httpx.Request:
        """Hook called on every outgoing request, including each retry. Override it.

        Use it to sign requests, add per-request headers or log. Auth from `auth=` is
        applied after this hook.

        Example:
            ```python
            def prepare_request(self, request, route):
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

    def process_response(
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

    # -- sending --------------------------------------------------------------

    def call[**P, ReqT, RespT](
        self, route: Route[P, ReqT, RespT], /, *args: P.args, **kwargs: P.kwargs
    ) -> RespT:
        """Call a route that isn't declared on the client class.

        Example:
            ```python
            get_user = nanoapi.get("/users/{user_id}", request=GetUser, response=User)
            user = client.call(get_user, user_id=1)
            ```

        Args:
            route: The route to call.
            *args: Positional arguments for the route's request model.
            **kwargs: Keyword arguments for the route's request model.

        Returns:
            The decoded response.
        """
        return self.send(route, route.build_model(*args, **kwargs))

    def send[ReqT, RespT](self, route: Route[Any, ReqT, RespT], request: ReqT) -> RespT:
        """Send an already-built request model and decode the response.

        Args:
            route: The route to call.
            request: An instance of the route's request model.

        Returns:
            The decoded response.

        Raises:
            APIStatusError: On a 4xx/5xx response (see `raise_for_status`).
            APIConnectionError: If no response arrived, after any retries.
            ResponseValidationError: If the body doesn't match the response type.
        """
        response = self.send_raw(route, request)
        self.raise_for_status(response)
        return route.parse(response)

    def send_raw[ReqT](self, route: Route[Any, ReqT, Any], request: ReqT) -> httpx.Response:
        """Send a request model and return the response without checking or decoding it.

        Hooks and retries still apply; `raise_for_status` doesn't.

        Args:
            route: The route to call.
            request: An instance of the route's request model.

        Returns:
            The response, whatever its status.

        Raises:
            APIConnectionError: If no response arrived, after any retries.
        """
        return self._send(route, request, None)

    def _send[ReqT](
        self, route: Route[Any, ReqT, Any], request: ReqT, url: httpx.URL | None
    ) -> httpx.Response:
        retry = self.retry_policy(route)
        attempt = 0
        while True:
            http_request = self.prepare_request(self.build_request(route, request, url=url), route)
            try:
                response = self._http.send(http_request)
            except httpx.TransportError as exc:
                if retry is None or not retry.should_retry(attempt, http_request, None):
                    raise self._wrap_transport_error(exc, http_request) from exc
                time.sleep(retry.delay(attempt, None))
            else:
                if retry is None or not retry.should_retry(attempt, http_request, response):
                    return self.process_response(response, route)
                response.close()
                time.sleep(retry.delay(attempt, response))
            attempt += 1

    # -- pagination -----------------------------------------------------------

    def iter[**P, ReqT, RespT, ItemT](
        self, route: PaginatedRoute[P, ReqT, RespT, ItemT], /, *args: P.args, **kwargs: P.kwargs
    ) -> Iterator[ItemT]:
        """Iterate over every item on every page of a paginated route.

        Arguments are validated immediately; pages are fetched as you iterate.

        Args:
            route: The paginated route.
            *args: Positional arguments for the first page's request model.
            **kwargs: Keyword arguments for the first page's request model.

        Returns:
            A lazy iterator over the items.
        """
        pages = self._pages(route, route.route.build_model(*args, **kwargs))
        return (item for page in pages for item in page.items)

    def pages[**P, ReqT, RespT, ItemT](
        self, route: PaginatedRoute[P, ReqT, RespT, ItemT], /, *args: P.args, **kwargs: P.kwargs
    ) -> Iterator[Page[ReqT, RespT, ItemT]]:
        """Iterate over every page of a paginated route.

        Args:
            route: The paginated route.
            *args: Positional arguments for the first page's request model.
            **kwargs: Keyword arguments for the first page's request model.

        Returns:
            A lazy iterator over [`Page`][nanoapi.Page] objects.
        """
        return self._pages(route, route.route.build_model(*args, **kwargs))

    def _pages[ReqT, RespT, ItemT](
        self, route: PaginatedRoute[Any, ReqT, RespT, ItemT], request: ReqT
    ) -> Iterator[Page[ReqT, RespT, ItemT]]:
        url: httpx.URL | None = None
        while True:
            response = self._send(route.route, request, url)
            self.raise_for_status(response)
            page = route.make_page(request, response)
            yield page
            step = self._next_page(route, page, url)
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

    The async twin of [`Client`][nanoapi.Client], with the same constructor arguments
    (`http_client` takes an `httpx.AsyncClient`). Route calls return awaitables,
    paginated routes give async iterators, and the `prepare_request` /
    `process_response` hooks are `async def`.

    ```python
    class AsyncGitHub(Users, Repos, nanoapi.AsyncClient): ...

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
        http_client: httpx.AsyncClient | None = None,
        **httpx_kwargs: Any,
    ) -> None:
        opts = self._setup(
            base_url, headers, timeout, auth, error_model, retry, http_client, httpx_kwargs
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

    async def prepare_request(
        self, request: httpx.Request, route: Route[Any, Any, Any]
    ) -> httpx.Request:
        """Hook called on every outgoing request, including each retry.

        The async version of [`Client.prepare_request`][nanoapi.Client.prepare_request].

        Args:
            request: The request about to be sent.
            route: The route being called.

        Returns:
            The request to send.
        """
        return request

    async def process_response(
        self, response: httpx.Response, route: Route[Any, Any, Any]
    ) -> httpx.Response:
        """Hook called on every final response, before the status check and decoding.

        The async version of [`Client.process_response`][nanoapi.Client.process_response].

        Args:
            response: The response.
            route: The route that was called.

        Returns:
            The response to check and decode.
        """
        return response

    # -- sending --------------------------------------------------------------

    async def call[**P, ReqT, RespT](
        self, route: Route[P, ReqT, RespT], /, *args: P.args, **kwargs: P.kwargs
    ) -> RespT:
        """Call a route that isn't declared on the client class; see `Client.call`.

        Args:
            route: The route to call.
            *args: Positional arguments for the route's request model.
            **kwargs: Keyword arguments for the route's request model.

        Returns:
            The decoded response.
        """
        return await self.send(route, route.build_model(*args, **kwargs))

    async def send[ReqT, RespT](self, route: Route[Any, ReqT, RespT], request: ReqT) -> RespT:
        """Send an already-built request model and decode the response; see `Client.send`.

        Args:
            route: The route to call.
            request: An instance of the route's request model.

        Returns:
            The decoded response.
        """
        response = await self.send_raw(route, request)
        self.raise_for_status(response)
        return route.parse(response)

    async def send_raw[ReqT](self, route: Route[Any, ReqT, Any], request: ReqT) -> httpx.Response:
        """Send without checking the status or decoding; see `Client.send_raw`.

        Args:
            route: The route to call.
            request: An instance of the route's request model.

        Returns:
            The response, whatever its status.
        """
        return await self._send(route, request, None)

    async def _send[ReqT](
        self, route: Route[Any, ReqT, Any], request: ReqT, url: httpx.URL | None
    ) -> httpx.Response:
        retry = self.retry_policy(route)
        attempt = 0
        while True:
            http_request = await self.prepare_request(
                self.build_request(route, request, url=url), route
            )
            try:
                response = await self._http.send(http_request)
            except httpx.TransportError as exc:
                if retry is None or not retry.should_retry(attempt, http_request, None):
                    raise self._wrap_transport_error(exc, http_request) from exc
                await asyncio.sleep(retry.delay(attempt, None))
            else:
                if retry is None or not retry.should_retry(attempt, http_request, response):
                    return await self.process_response(response, route)
                await response.aclose()
                await asyncio.sleep(retry.delay(attempt, response))
            attempt += 1

    # -- pagination -----------------------------------------------------------

    def iter[**P, ReqT, RespT, ItemT](
        self, route: PaginatedRoute[P, ReqT, RespT, ItemT], /, *args: P.args, **kwargs: P.kwargs
    ) -> AsyncIterator[ItemT]:
        """Iterate over every item on every page: `async for post in client.iter(route)`.

        Arguments are validated immediately; pages are fetched as you iterate.

        Args:
            route: The paginated route.
            *args: Positional arguments for the first page's request model.
            **kwargs: Keyword arguments for the first page's request model.

        Returns:
            A lazy async iterator over the items.
        """
        pages = self._pages(route, route.route.build_model(*args, **kwargs))
        return self._items(pages)

    def pages[**P, ReqT, RespT, ItemT](
        self, route: PaginatedRoute[P, ReqT, RespT, ItemT], /, *args: P.args, **kwargs: P.kwargs
    ) -> AsyncIterator[Page[ReqT, RespT, ItemT]]:
        """Iterate over every page of a paginated route: `async for page in ...`.

        Args:
            route: The paginated route.
            *args: Positional arguments for the first page's request model.
            **kwargs: Keyword arguments for the first page's request model.

        Returns:
            A lazy async iterator over [`Page`][nanoapi.Page] objects.
        """
        return self._pages(route, route.route.build_model(*args, **kwargs))

    @staticmethod
    async def _items[ItemT](pages: AsyncIterator[Page[Any, Any, ItemT]]) -> AsyncIterator[ItemT]:
        async for page in pages:
            for item in page.items:
                yield item

    async def _pages[ReqT, RespT, ItemT](
        self, route: PaginatedRoute[Any, ReqT, RespT, ItemT], request: ReqT
    ) -> AsyncIterator[Page[ReqT, RespT, ItemT]]:
        url: httpx.URL | None = None
        while True:
            response = await self._send(route.route, request, url)
            self.raise_for_status(response)
            page = route.make_page(request, response)
            yield page
            step = self._next_page(route, page, url)
            if step is None:
                return
            request, url = step

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
    """A route attached to a [`Client`][nanoapi.Client]; what `api.some_route` gives you.

    Call it with the request model's arguments to send the request and get the decoded
    response. The other methods are variations for when you need more control.

    ```python
    api.get_user(user_id=1)                 # -> User
    api.get_user.send(GetUser(user_id=1))   # already-built model
    api.get_user.raw(user_id=1)             # httpx.Response, no status check / decoding
    api.get_user.build_request(user_id=1)   # httpx.Request, not sent
    ```

    Attributes:
        route: The underlying [`Route`][nanoapi.Route].
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
        return self.client.send(self.route, self.route.build_model(*args, **kwargs))

    def send(self, request: ReqT) -> RespT:
        """Send an already-built request model and decode the response.

        Args:
            request: An instance of the route's request model.

        Returns:
            The decoded response.
        """
        return self.client.send(self.route, request)

    def raw(self, *args: P.args, **kwargs: P.kwargs) -> httpx.Response:
        """Like calling the route, but return the `httpx.Response` as-is.

        Hooks and retries apply; there's no status check and no decoding.

        Returns:
            The response, whatever its status.
        """
        return self.client.send_raw(self.route, self.route.build_model(*args, **kwargs))

    def build_request(self, *args: P.args, **kwargs: P.kwargs) -> httpx.Request:
        """Build the request that calling the route would send, without sending it.

        Auth and the `prepare_request` hook aren't applied; they run at send time.

        Returns:
            The unsent request.
        """
        return self.client.build_request(self.route, self.route.build_model(*args, **kwargs))

    def __repr__(self) -> str:
        return f"<bound {self.route!r}>"


class BoundPaginatedRoute[**P, ReqT, RespT, ItemT](BoundRoute[P, ReqT, RespT]):
    """A paginated route attached to a [`Client`][nanoapi.Client].

    Calling it fetches just the first page, like any [`BoundRoute`][nanoapi.BoundRoute];
    `iter` and `pages` walk all of them.

    ```python
    first = api.list_posts(limit=50)            # the first page's response
    for post in api.list_posts.iter(limit=50):  # every post, page by page
        ...
    ```

    Attributes:
        paginated: The underlying [`PaginatedRoute`][nanoapi.PaginatedRoute].
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
        return self.client.iter(self.paginated, *args, **kwargs)

    def pages(self, *args: P.args, **kwargs: P.kwargs) -> Iterator[Page[ReqT, RespT, ItemT]]:
        """Iterate over every page, with each page's request, data, items and response.

        Returns:
            A lazy iterator over [`Page`][nanoapi.Page] objects.
        """
        return self.client.pages(self.paginated, *args, **kwargs)


class AsyncBoundRoute[**P, ReqT, RespT]:
    """A route attached to an [`AsyncClient`][nanoapi.AsyncClient].

    The async twin of [`BoundRoute`][nanoapi.BoundRoute]: calling it, `send` and `raw`
    return awaitables.

    Attributes:
        route: The underlying [`Route`][nanoapi.Route].
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
        return await self.client.send(self.route, self.route.build_model(*args, **kwargs))

    async def send(self, request: ReqT) -> RespT:
        """Send an already-built request model and decode the response.

        Args:
            request: An instance of the route's request model.

        Returns:
            The decoded response.
        """
        return await self.client.send(self.route, request)

    async def raw(self, *args: P.args, **kwargs: P.kwargs) -> httpx.Response:
        """Like calling the route, but return the `httpx.Response` as-is.

        Returns:
            The response, whatever its status.
        """
        return await self.client.send_raw(self.route, self.route.build_model(*args, **kwargs))

    def build_request(self, *args: P.args, **kwargs: P.kwargs) -> httpx.Request:
        """Build the request that calling the route would send, without sending it.

        Returns:
            The unsent request.
        """
        return self.client.build_request(self.route, self.route.build_model(*args, **kwargs))

    def __repr__(self) -> str:
        return f"<bound async {self.route!r}>"


class AsyncBoundPaginatedRoute[**P, ReqT, RespT, ItemT](AsyncBoundRoute[P, ReqT, RespT]):
    """A paginated route attached to an [`AsyncClient`][nanoapi.AsyncClient].

    ```python
    async for post in api.list_posts.iter(limit=50):
        ...
    ```

    Attributes:
        paginated: The underlying [`PaginatedRoute`][nanoapi.PaginatedRoute].
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
        return self.client.iter(self.paginated, *args, **kwargs)

    def pages(self, *args: P.args, **kwargs: P.kwargs) -> AsyncIterator[Page[ReqT, RespT, ItemT]]:
        """Iterate over every page with `async for`.

        Returns:
            A lazy async iterator over [`Page`][nanoapi.Page] objects.
        """
        return self.client.pages(self.paginated, *args, **kwargs)

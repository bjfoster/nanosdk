from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import TypeAdapter, ValidationError

from ._errors import APIConnectionError, APIStatusError, APITimeoutError
from ._paginate import Page
from ._retry import Retry
from ._route import PaginatedRoute, Route, Routes

__all__ = [
    "AsyncBoundPaginatedRoute",
    "AsyncBoundRoute",
    "AsyncClient",
    "BoundPaginatedRoute",
    "BoundRoute",
    "Client",
]

type TimeoutTypes = float | httpx.Timeout | None
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class _NotGiven:
    def __repr__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN = _NotGiven()


class _BaseClient[HttpT: (httpx.Client, httpx.AsyncClient)](Routes):
    """Shared plumbing for ``Client`` and ``AsyncClient``."""

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
        """A copy with extra headers / a different timeout or retry policy.

        The copy shares the connection pool and never closes it; close the original.
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
        """The underlying httpx client."""
        return self._http

    # -- overridable behaviour ----------------------------------------------

    def retry_policy(self, route: Route[Any, Any, Any]) -> Retry | None:
        """The retry policy for ``route``. Default: the client's ``retry=``.

        Override to vary it per route, e.g. ``None if route.meta.get("no_retry") else ...``.
        """
        return self._retry

    def raise_for_status(self, response: httpx.Response) -> None:
        """Override to customise error handling. Default: raise ``APIStatusError`` on 4xx/5xx."""
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
        """Build (but don't send) the ``httpx.Request`` for ``route`` + ``request``.

        ``url`` replaces the route's URL and query string (used to follow pagination links).
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
        """Where the page after ``page`` comes from, or ``None`` when done (or stuck)."""
        nxt = route.next_page(page)
        if nxt is None:
            return None
        if isinstance(nxt, httpx.URL):
            return None if nxt == page.response.request.url else (page.request, nxt)
        return None if (nxt == page.request and url is None) else (nxt, None)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} base_url={str(self._http.base_url)!r}>"


class Client(_BaseClient[httpx.Client]):
    """Synchronous API client.

    Subclass it and declare routes as class attributes::

        class GitHub(nanoapi.Client):
            get_user = nanoapi.get("/users/{username}", request=GetUser, response=User)

        gh = GitHub("https://api.github.com")
        user = gh.get_user(username="octocat")  # -> User
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
        """Called on every outgoing request (each retry too). Sign, log, rewrite..."""
        return request

    def process_response(
        self, response: httpx.Response, route: Route[Any, Any, Any]
    ) -> httpx.Response:
        """Called on every final response, before the status check and decoding."""
        return response

    # -- sending --------------------------------------------------------------

    def call[**P, ReqT, RespT](
        self, route: Route[P, ReqT, RespT], /, *args: P.args, **kwargs: P.kwargs
    ) -> RespT:
        """Call any route without declaring it on the class: ``client.call(get_user, id=1)``."""
        return self.send(route, route.build_model(*args, **kwargs))

    def send[ReqT, RespT](self, route: Route[Any, ReqT, RespT], request: ReqT) -> RespT:
        """Send a pre-built request model and decode the response."""
        response = self.send_raw(route, request)
        self.raise_for_status(response)
        return route.parse(response)

    def send_raw[ReqT](self, route: Route[Any, ReqT, Any], request: ReqT) -> httpx.Response:
        """Send (with hooks and retries) and return the ``httpx.Response`` undecoded."""
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
        """Every item on every page, fetched lazily."""
        pages = self._pages(route, route.route.build_model(*args, **kwargs))
        return (item for page in pages for item in page.items)

    def pages[**P, ReqT, RespT, ItemT](
        self, route: PaginatedRoute[P, ReqT, RespT, ItemT], /, *args: P.args, **kwargs: P.kwargs
    ) -> Iterator[Page[ReqT, RespT, ItemT]]:
        """Every page, fetched lazily."""
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
    """Async twin of ``Client``: every route call returns an awaitable."""

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
        """Called on every outgoing request (each retry too). Sign, log, rewrite..."""
        return request

    async def process_response(
        self, response: httpx.Response, route: Route[Any, Any, Any]
    ) -> httpx.Response:
        """Called on every final response, before the status check and decoding."""
        return response

    # -- sending --------------------------------------------------------------

    async def call[**P, ReqT, RespT](
        self, route: Route[P, ReqT, RespT], /, *args: P.args, **kwargs: P.kwargs
    ) -> RespT:
        return await self.send(route, route.build_model(*args, **kwargs))

    async def send[ReqT, RespT](self, route: Route[Any, ReqT, RespT], request: ReqT) -> RespT:
        response = await self.send_raw(route, request)
        self.raise_for_status(response)
        return route.parse(response)

    async def send_raw[ReqT](self, route: Route[Any, ReqT, Any], request: ReqT) -> httpx.Response:
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
        """Every item on every page: ``async for post in client.iter(route, ...)``."""
        pages = self._pages(route, route.route.build_model(*args, **kwargs))
        return self._items(pages)

    def pages[**P, ReqT, RespT, ItemT](
        self, route: PaginatedRoute[P, ReqT, RespT, ItemT], /, *args: P.args, **kwargs: P.kwargs
    ) -> AsyncIterator[Page[ReqT, RespT, ItemT]]:
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
    """A route attached to a ``Client``. Call it like the request model's constructor."""

    __slots__ = ("client", "route")

    def __init__(self, route: Route[P, ReqT, RespT], client: Client) -> None:
        self.route = route
        self.client = client

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> RespT:
        return self.client.send(self.route, self.route.build_model(*args, **kwargs))

    def send(self, request: ReqT) -> RespT:
        """Send an already-built request model."""
        return self.client.send(self.route, request)

    def raw(self, *args: P.args, **kwargs: P.kwargs) -> httpx.Response:
        """Like calling the route, but returns the raw ``httpx.Response`` (no status check)."""
        return self.client.send_raw(self.route, self.route.build_model(*args, **kwargs))

    def build_request(self, *args: P.args, **kwargs: P.kwargs) -> httpx.Request:
        """The ``httpx.Request`` that calling the route would send (before ``prepare_request``)."""
        return self.client.build_request(self.route, self.route.build_model(*args, **kwargs))

    def __repr__(self) -> str:
        return f"<bound {self.route!r}>"


class BoundPaginatedRoute[**P, ReqT, RespT, ItemT](BoundRoute[P, ReqT, RespT]):
    """A paginated route attached to a ``Client``. Calling it fetches the first page."""

    __slots__ = ("paginated",)

    def __init__(self, paginated: PaginatedRoute[P, ReqT, RespT, ItemT], client: Client) -> None:
        super().__init__(paginated.route, client)
        self.paginated = paginated

    def iter(self, *args: P.args, **kwargs: P.kwargs) -> Iterator[ItemT]:
        """Every item on every page, fetched lazily."""
        return self.client.iter(self.paginated, *args, **kwargs)

    def pages(self, *args: P.args, **kwargs: P.kwargs) -> Iterator[Page[ReqT, RespT, ItemT]]:
        """Every page, fetched lazily."""
        return self.client.pages(self.paginated, *args, **kwargs)


class AsyncBoundRoute[**P, ReqT, RespT]:
    """A route attached to an ``AsyncClient``. Calling it returns an awaitable."""

    __slots__ = ("client", "route")

    def __init__(self, route: Route[P, ReqT, RespT], client: AsyncClient) -> None:
        self.route = route
        self.client = client

    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> RespT:
        return await self.client.send(self.route, self.route.build_model(*args, **kwargs))

    async def send(self, request: ReqT) -> RespT:
        return await self.client.send(self.route, request)

    async def raw(self, *args: P.args, **kwargs: P.kwargs) -> httpx.Response:
        return await self.client.send_raw(self.route, self.route.build_model(*args, **kwargs))

    def build_request(self, *args: P.args, **kwargs: P.kwargs) -> httpx.Request:
        return self.client.build_request(self.route, self.route.build_model(*args, **kwargs))

    def __repr__(self) -> str:
        return f"<bound async {self.route!r}>"


class AsyncBoundPaginatedRoute[**P, ReqT, RespT, ItemT](AsyncBoundRoute[P, ReqT, RespT]):
    """A paginated route attached to an ``AsyncClient``. Use with ``async for``."""

    __slots__ = ("paginated",)

    def __init__(
        self, paginated: PaginatedRoute[P, ReqT, RespT, ItemT], client: AsyncClient
    ) -> None:
        super().__init__(paginated.route, client)
        self.paginated = paginated

    def iter(self, *args: P.args, **kwargs: P.kwargs) -> AsyncIterator[ItemT]:
        return self.client.iter(self.paginated, *args, **kwargs)

    def pages(self, *args: P.args, **kwargs: P.kwargs) -> AsyncIterator[Page[ReqT, RespT, ItemT]]:
        return self.client.pages(self.paginated, *args, **kwargs)

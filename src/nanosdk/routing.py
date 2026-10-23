"""Declaring routes: `get`/`post`/..., `Route`, `PaginatedRoute` and `Routes` groups.

A route ties an HTTP method and path to a request model and a response type:

```python
class Api(nanosdk.Client):
    get_user = nanosdk.get("/users/{user_id}", request=GetUser, response=User)
```

On a client *instance*, `api.get_user` is a bound route you call like the request model's
constructor; the result is the decoded response. See [Where fields go][nanosdk.params]
for how request fields are placed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any, Self, cast, overload
from urllib.parse import quote

import httpx
from pydantic import BaseModel, RootModel, TypeAdapter, ValidationError

from .errors import ResponseValidationError
from .pagination import NextPage, Page
from .params import Location, Param

if TYPE_CHECKING:
    from .client import (
        AsyncBoundPaginatedRoute,
        AsyncBoundRoute,
        AsyncClient,
        BoundPaginatedRoute,
        BoundRoute,
        Client,
    )

__all__ = [
    "PaginatedRoute",
    "Route",
    "Routes",
    "Verb",
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "route",
]

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_PLACEHOLDER = re.compile(r"{([A-Za-z_][A-Za-z0-9_]*)}")


@dataclass(slots=True)
class EncodedRequest:
    """Everything needed to build an `httpx.Request` for one call.

    Produced by [`Route.encode`][nanosdk.Route.encode]; the client turns it into an
    `httpx.Request`.

    Attributes:
        method: The HTTP method.
        url: The path with placeholders filled in (relative to the client's `base_url`).
        params: Query parameters as `(name, value)` pairs, in order; repeats allowed.
        headers: Route headers plus header fields from the request model.
        json: The JSON body, when `has_json` is set.
        content: A raw `str` or `bytes` body.
        data: Form fields.
        files: Multipart files.
        has_json: Whether `json` holds a body (it may legitimately be `None`).
    """

    method: str
    url: str
    params: list[tuple[str, Any]] = field(default_factory=list[tuple[str, Any]])
    headers: dict[str, str] = field(default_factory=dict[str, str])
    json: Any = None
    content: bytes | str | None = None
    data: dict[str, Any] | None = None
    files: dict[str, Any] | None = None
    has_json: bool = False


@dataclass(frozen=True, slots=True)
class _FieldPlan:
    name: str  # python attribute name
    key: str  # key in model_dump(by_alias=True)
    wire: str  # name on the wire
    location: Location


@dataclass(frozen=True, slots=True)
class _Plan:
    fields: tuple[_FieldPlan, ...]
    root_body: bool  # request model is a RootModel -> whole dump is the body


class Route[**P, ReqT, RespT]:
    """One HTTP endpoint: method, path, request model and response type.

    Usually made with [`get`][nanosdk.get], [`post`][nanosdk.post] and friends rather than
    directly. As a class attribute of a [`Client`][nanosdk.Client] it's a descriptor: on
    an instance it becomes a [`BoundRoute`][nanosdk.BoundRoute] you call with the request
    model's arguments.

    The type parameters carry the typing end to end: `P` is the request model's
    constructor signature (so calls are checked like constructing the model), `ReqT` the
    request model and `RespT` the decoded response type.

    Args:
        method: The HTTP method; any case.
        path: The path, relative to the client's `base_url`, with `{name}` placeholders
            filled from request-model fields.
        request: The request model: a pydantic `BaseModel` (or `RootModel`) subclass.
            `None` for routes that take no arguments.
        response: The response type: anything pydantic's `TypeAdapter` accepts (a model,
            `list[Model]`, `dict[str, int]`, ...), or `str`, `bytes`, `httpx.Response`,
            or `None` to ignore the body.
        headers: Static headers sent with every call of this route.
        exclude_unset: Only send fields the caller actually set. Defaults to `True` for
            PATCH and `False` otherwise.
        name: The route's name; defaults to the attribute name it's assigned to.
        meta: Free-form data for your client hooks, such as `{"auth": False}`.

    Attributes:
        method: The upper-cased HTTP method.
        path: The path template.
        request: The request model class, or `None`.
        response: The declared response type.
        headers: Static headers.
        exclude_unset: Whether only explicitly set fields are sent.
        name: The route's name (the attribute name on its class, once assigned).
        meta: Free-form data for client hooks.
    """

    def __init__(
        self,
        method: str,
        path: str,
        *,
        request: Callable[P, ReqT] | None = None,
        response: Any = None,
        headers: Mapping[str, str] | None = None,
        exclude_unset: bool | None = None,
        name: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        self.method = method.upper()
        self.path = path
        self.request = request
        self.response = response
        self.headers: dict[str, str] = dict(headers or {})
        # PATCH bodies only send fields you actually set, unless told otherwise.
        self.exclude_unset = self.method == "PATCH" if exclude_unset is None else exclude_unset
        self.name = name
        # Free-form data for your client hooks (prepare_request, retry_policy, ...).
        self.meta: dict[str, Any] = dict(meta or {})
        self._maps: tuple[Callable[[Any], Any], ...] = ()

    # -- descriptor protocol: Route on a Client subclass becomes a bound, callable route --

    def __set_name__(self, owner: type, name: str) -> None:
        if self.name is None:
            self.name = name

    @overload
    def __get__(self, instance: None, owner: type[Any]) -> Self: ...
    @overload
    def __get__(self, instance: Client, owner: type[Any]) -> BoundRoute[P, ReqT, RespT]: ...
    @overload
    def __get__(
        self, instance: AsyncClient, owner: type[Any]
    ) -> AsyncBoundRoute[P, ReqT, RespT]: ...
    def __get__(self, instance: object, owner: type[Any]) -> Any:
        bind = getattr(instance, "_bind_route", None)
        return self if bind is None else bind(self)

    @overload
    def bind(self, client: Client) -> BoundRoute[P, ReqT, RespT]: ...
    @overload
    def bind(self, client: AsyncClient) -> AsyncBoundRoute[P, ReqT, RespT]: ...
    def bind(self, client: Client | AsyncClient) -> Any:
        """Attach the route to a client, as accessing it on a client class does.

        For routes that aren't declared on the client's class:

        ```python
        get_user = nanosdk.get("/users/{user_id}", request=GetUser, response=User)
        user = get_user.bind(api)(user_id=1)
        ```

        Args:
            client: The client to send through.

        Returns:
            A bound route: call it, or use `.send()`, `.raw()`, `.build_request()`.
        """
        return self.__get__(client, type(client))

    # -- helpers ------------------------------------------------------------

    def _clone(self) -> Self:
        clone = type(self).__new__(type(self))
        clone.__dict__.update(
            {k: v for k, v in self.__dict__.items() if k not in ("_plan", "_adapter")}
        )
        return clone

    def with_prefix(self, prefix: str) -> Self:
        """Return a copy with `prefix` prepended to the path.

        [`Routes`][nanosdk.Routes] groups call this for their `prefix=`.

        Args:
            prefix: Path prefix such as `"/users"`; slashes are normalised.

        Returns:
            A new route; this one is unchanged.
        """
        clone = self._clone()
        clone.path = prefix.rstrip("/") + "/" + self.path.lstrip("/") if self.path else prefix
        return clone

    def map[U](self, fn: Callable[[RespT], U]) -> Route[P, ReqT, U]:
        """Return a copy whose decoded response is passed through `fn`.

        The return type follows `fn`, so type checkers see the mapped type. Maps chain,
        and run after validation, so `fn` receives the declared response type.

        Example:
            ```python
            count = nanosdk.get("/posts", response=list[Post]).map(len)       # -> int
            frame = nanosdk.get("/posts", response=list[Post]).map(frames.polars)
            ```

        Args:
            fn: Function of the decoded response.

        Returns:
            A new route; this one is unchanged.
        """
        clone = cast(Route[Any, Any, Any], self._clone())
        clone._maps = (*self._maps, fn)
        return clone

    @overload
    def paginate[I](
        self: Route[P, ReqT, list[I]], next: NextPage[ReqT, list[I], I]
    ) -> PaginatedRoute[P, ReqT, list[I], I]: ...
    @overload
    def paginate[I](
        self, next: NextPage[ReqT, RespT, Any], *, items: Callable[[RespT], Iterable[I]]
    ) -> PaginatedRoute[P, ReqT, RespT, I]: ...
    def paginate(self, next: Any, *, items: Any = None) -> PaginatedRoute[Any, Any, Any, Any]:
        """Make the route iterable across pages.

        Bound to a client, the result keeps the route's normal call (the first page)
        and adds `.iter()` (every item, lazily) and `.pages()` (every
        [`Page`][nanosdk.Page]).

        Example:
            ```python
            # The response is a list: its elements are the items.
            by_offset = nanosdk.get("/posts", request=ListPosts, response=list[Post]).paginate(
                nanosdk.Offset("offset", limit="limit")
            )
            # The response wraps the list: say where the items are.
            feed = nanosdk.get("/feed", request=Feed, response=FeedPage).paginate(
                nanosdk.Cursor("next_cursor"), items=lambda page: page.data
            )
            ```

        Args:
            next: The pagination strategy: [`Cursor`][nanosdk.Cursor],
                [`Offset`][nanosdk.Offset], [`PageNumber`][nanosdk.PageNumber],
                [`LinkHeader`][nanosdk.LinkHeader], or any function of the current
                `Page` returning the next request model, an `httpx.URL`, or `None` to stop.
            items: Extracts the item list from a decoded page. Optional when the
                response type is already a `list`.

        Returns:
            A paginated route wrapping this one.
        """
        return PaginatedRoute(self, items=items or _as_items, next=next)

    def __repr__(self) -> str:
        req = getattr(self.request, "__name__", None)
        resp = getattr(self.response, "__name__", repr(self.response))
        return f"<Route {self.name or '?'}: {self.method} {self.path} ({req}) -> {resp}>"

    def build_model(self, *args: P.args, **kwargs: P.kwargs) -> ReqT:
        """Construct the request model from call arguments.

        Raises:
            pydantic.ValidationError: If the arguments don't validate.
            TypeError: If the route has no request model but arguments were given.
        """
        if self.request is None:
            if args or kwargs:
                raise TypeError(f"{self.name or self.path}() takes no arguments")
            return cast(ReqT, None)
        return self.request(*args, **kwargs)

    # -- request encoding ---------------------------------------------------

    @cached_property
    def _plan(self) -> _Plan:
        placeholders = set(_PLACEHOLDER.findall(self.path))
        req = self.request
        if req is None:
            if placeholders:
                raise TypeError(f"route {self.path!r} has path params but no request model")
            return _Plan((), root_body=False)
        if not (isinstance(req, type) and issubclass(req, BaseModel)):
            raise TypeError(f"request must be a pydantic BaseModel subclass, got {req!r}")
        if issubclass(req, RootModel):
            if placeholders:
                raise TypeError("a RootModel request can't fill path params")
            return _Plan((), root_body=True)

        default_loc = Location.JSON if self.method in _BODY_METHODS else Location.QUERY
        plans: list[_FieldPlan] = []
        for name, info in req.model_fields.items():
            marker = next((m for m in info.metadata if isinstance(m, Param)), None)
            key = info.serialization_alias or info.alias or name
            if marker is not None:
                loc = marker.location
            elif name in placeholders or key in placeholders:
                loc = Location.PATH
            else:
                loc = default_loc
            wire = (marker.alias if marker else None) or key
            if loc is Location.PATH and wire not in placeholders and name in placeholders:
                wire = name
            if loc is Location.HEADER and not (marker and marker.alias):
                wire = wire.replace("_", "-")
            plans.append(_FieldPlan(name, key, wire, loc))

        missing = placeholders - {p.wire for p in plans if p.location is Location.PATH}
        if missing:
            raise TypeError(
                f"route {self.path!r}: no request field for path params {sorted(missing)}"
            )
        if sum(p.location is Location.BODY for p in plans) > 1:
            raise TypeError("only one field can be marked Body()")
        return _Plan(tuple(plans), root_body=False)

    def encode(self, model: ReqT) -> EncodedRequest:
        """Work out the URL, query, headers and body for a request model.

        Args:
            model: An instance of the route's request model (`None` if it has none).

        Returns:
            The encoded request, before client-level headers and auth are added.

        Raises:
            TypeError: If the model's fields can't fill the route's path placeholders,
                or the `Body()` / `Json()` markers conflict.
        """
        plan = self._plan
        out = EncodedRequest(self.method, self.path, headers=dict(self.headers))
        if model is None:
            if plan.fields or plan.root_body:
                raise TypeError(f"{self.name or self.path}: a request model is required")
            return out
        assert isinstance(model, BaseModel)

        if plan.root_body:
            out.json = model.model_dump(mode="json", by_alias=True)
            out.has_json = True
            return out

        file_names = {p.name for p in plan.fields if p.location is Location.FILE}
        dumped = model.model_dump(
            mode="json", by_alias=True, exclude=file_names or None, exclude_unset=self.exclude_unset
        )
        path_values: dict[str, str] = {}
        json_body: dict[str, Any] = {}
        form: dict[str, Any] = {}
        files: dict[str, Any] = {}

        for p in plan.fields:
            if p.location is Location.FILE:
                value = getattr(model, p.name)
                if value is not None:
                    files[p.wire] = value
                continue
            if p.key not in dumped:  # excluded by exclude_unset
                continue
            value = dumped[p.key]
            loc = p.location
            if loc is Location.PATH:
                path_values[p.wire] = quote(_scalar(value), safe="")
            elif loc is Location.QUERY:
                if value is None:
                    continue
                if isinstance(value, (list, tuple)):
                    out.params.extend((p.wire, _scalar(v)) for v in _iter(value))
                else:
                    out.params.append((p.wire, _scalar(value)))
            elif loc is Location.HEADER:
                if value is not None:
                    out.headers[p.wire] = _scalar(value)
            elif loc is Location.JSON:
                json_body[p.wire] = value
            elif loc is Location.FORM:
                if value is not None:
                    form[p.wire] = value
            elif loc is Location.BODY:
                raw = getattr(model, p.name)
                if isinstance(raw, (bytes, str)):
                    out.content = raw
                else:
                    out.json = value
                    out.has_json = True

        out.url = _PLACEHOLDER.sub(lambda m: path_values[m.group(1)], self.path)
        if json_body:
            if out.has_json:
                raise TypeError("can't combine a Body() field with other JSON body fields")
            out.json = json_body
            out.has_json = True
        if form:
            out.data = form
        if files:
            out.files = files
        return out

    # -- response decoding --------------------------------------------------

    @cached_property
    def _adapter(self) -> TypeAdapter[Any]:
        return TypeAdapter(self.response)

    def parse(self, response: httpx.Response) -> RespT:
        """Decode a successful response into the declared type, then apply any maps.

        Args:
            response: A 2xx response.

        Returns:
            The decoded (and mapped) value.

        Raises:
            ResponseValidationError: If the body doesn't match the response type.
        """
        value: Any = self._decode(response)
        for fn in self._maps:
            value = fn(value)
        return cast(RespT, value)

    def _decode(self, response: httpx.Response) -> Any:
        t = self.response
        if t is None:
            return None
        if t is httpx.Response:
            return response
        if t is bytes:
            return response.content
        if t is str:
            return response.text
        try:
            if not response.content:
                return self._adapter.validate_python(None)
            return self._adapter.validate_json(response.content)
        except ValidationError as exc:
            raise ResponseValidationError(response, exc) from exc


def _as_items(data: Any) -> Iterable[Any]:
    return cast(Iterable[Any], data)  # a list response is already its own items


def _iter(value: Any) -> list[Any]:
    return list(value)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Verb:
    """Declares routes for one HTTP method; `nanosdk.get`, `nanosdk.post` and so on.

    Call it with a path and the request/response types to get a [`Route`][nanosdk.Route].
    Overloads give the route precise types whether or not `request` and `response` are
    given. Use [`route`][nanosdk.route] for methods without a ready-made verb.

    Args:
        method: The HTTP method this verb declares.
    """

    def __init__(self, method: str) -> None:
        self.method = method

    @overload
    def __call__[**P, ReqT, RespT](
        self,
        path: str,
        *,
        request: Callable[P, ReqT],
        response: type[RespT],
        headers: Mapping[str, str] | None = ...,
        exclude_unset: bool | None = ...,
        meta: Mapping[str, Any] | None = ...,
    ) -> Route[P, ReqT, RespT]: ...
    @overload
    def __call__[**P, ReqT](
        self,
        path: str,
        *,
        request: Callable[P, ReqT],
        response: None = ...,
        headers: Mapping[str, str] | None = ...,
        exclude_unset: bool | None = ...,
        meta: Mapping[str, Any] | None = ...,
    ) -> Route[P, ReqT, None]: ...
    @overload
    def __call__[RespT](
        self,
        path: str,
        *,
        request: None = ...,
        response: type[RespT],
        headers: Mapping[str, str] | None = ...,
        exclude_unset: bool | None = ...,
        meta: Mapping[str, Any] | None = ...,
    ) -> Route[[], None, RespT]: ...
    @overload
    def __call__(
        self,
        path: str,
        *,
        request: None = ...,
        response: None = ...,
        headers: Mapping[str, str] | None = ...,
        exclude_unset: bool | None = ...,
        meta: Mapping[str, Any] | None = ...,
    ) -> Route[[], None, None]: ...
    def __call__(
        self,
        path: str,
        *,
        request: Any = None,
        response: Any = None,
        headers: Mapping[str, str] | None = None,
        exclude_unset: bool | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> Route[Any, Any, Any]:
        """Declare a route for this verb's method.

        Example:
            ```python
            get_user = nanosdk.get("/users/{user_id}", request=GetUser, response=User)
            health = nanosdk.get("/health", response=str, headers={"X-Probe": "1"})
            ```

        Args:
            path: The path, relative to the client's `base_url`, with `{name}`
                placeholders filled from request-model fields.
            request: The request model class; `None` for routes without arguments.
            response: The response type (see [`Route`][nanosdk.Route]); `None` ignores
                the body.
            headers: Static headers sent with every call.
            exclude_unset: Only send fields the caller set; defaults to `True` for PATCH.
            meta: Free-form data for client hooks.

        Returns:
            The route, typed from `request` and `response`.
        """
        return Route(
            self.method,
            path,
            request=request,
            response=response,
            headers=headers,
            exclude_unset=exclude_unset,
            meta=meta,
        )


get = Verb("GET")
"""Declare a GET route. Unmarked request fields go in the query string."""
post = Verb("POST")
"""Declare a POST route. Unmarked request fields become keys of a JSON body."""
put = Verb("PUT")
"""Declare a PUT route. Unmarked request fields become keys of a JSON body."""
patch = Verb("PATCH")
"""Declare a PATCH route. Only fields the caller sets are sent (`exclude_unset`)."""
delete = Verb("DELETE")
"""Declare a DELETE route. Unmarked request fields go in the query string."""
head = Verb("HEAD")
"""Declare a HEAD route."""
options = Verb("OPTIONS")
"""Declare an OPTIONS route."""


def route(method: str) -> Verb:
    """Get a [`Verb`][nanosdk.Verb] for any HTTP method.

    Example:
        ```python
        propfind = nanosdk.route("PROPFIND")("/dav/{path}", request=DavPath, response=str)
        ```

    Args:
        method: The HTTP method; any case.

    Returns:
        A verb that declares routes with that method.
    """
    return Verb(method.upper())


class Routes:
    """Base for a reusable group of routes, optionally under a common path prefix.

    Mix groups into a [`Client`][nanosdk.Client] and an
    [`AsyncClient`][nanosdk.AsyncClient] to share one set of route declarations between
    a sync and an async SDK.

    Example:
        ```python
        class Users(nanosdk.Routes, prefix="/users"):
            get = nanosdk.get("/{user_id}", request=GetUser, response=User)  # /users/{user_id}

        class Api(Users, Billing, nanosdk.Client): ...
        class AsyncApi(Users, Billing, nanosdk.AsyncClient): ...
        ```

    Pass `prefix=` as a class keyword to prepend it to every route declared in the class
    body.
    """

    def __init_subclass__(cls, *, prefix: str = "", **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if prefix:
            for attr, value in list(vars(cls).items()):
                if isinstance(value, Route | PaginatedRoute):
                    setattr(cls, attr, value.with_prefix(prefix))


class PaginatedRoute[**P, ReqT, RespT, ItemT]:
    """A route plus the rules for walking its pages.

    Made by [`Route.paginate`][nanosdk.Route.paginate].

    Bound to a client it becomes a [`BoundPaginatedRoute`][nanosdk.BoundPaginatedRoute]
    (or the async twin): calling it still fetches just the first page, and `.iter(...)`
    / `.pages(...)` walk all of them. `ItemT` is the type of each item.

    Args:
        route: The underlying route, which fetches one page.
        items: Extracts the item list from a decoded page.
        next: The strategy that picks the next page's request.

    Attributes:
        route: The underlying route.
        items: Extracts the item list from a decoded page.
        next_page: The strategy that picks the next page's request.
    """

    def __init__(
        self,
        route: Route[P, ReqT, RespT],
        *,
        items: Callable[[RespT], Iterable[ItemT]],
        next: NextPage[ReqT, RespT, ItemT],
    ) -> None:
        self.route = route
        self.items = items
        self.next_page = next

    def __set_name__(self, owner: type, name: str) -> None:
        self.route.__set_name__(owner, name)

    @overload
    def __get__(self, instance: None, owner: type[Any]) -> Self: ...
    @overload
    def __get__(
        self, instance: Client, owner: type[Any]
    ) -> BoundPaginatedRoute[P, ReqT, RespT, ItemT]: ...
    @overload
    def __get__(
        self, instance: AsyncClient, owner: type[Any]
    ) -> AsyncBoundPaginatedRoute[P, ReqT, RespT, ItemT]: ...
    def __get__(self, instance: object, owner: type[Any]) -> Any:
        bind = getattr(instance, "_bind_paginated", None)
        return self if bind is None else bind(self)

    @overload
    def bind(self, client: Client) -> BoundPaginatedRoute[P, ReqT, RespT, ItemT]: ...
    @overload
    def bind(self, client: AsyncClient) -> AsyncBoundPaginatedRoute[P, ReqT, RespT, ItemT]: ...
    def bind(self, client: Client | AsyncClient) -> Any:
        """Attach the paginated route to a client: `route.bind(api).iter(...)`.

        Args:
            client: The client to send through.

        Returns:
            A bound paginated route.
        """
        return self.__get__(client, type(client))

    def with_prefix(self, prefix: str) -> Self:
        """Return a copy whose underlying route has `prefix` prepended to its path."""
        return type(self)(self.route.with_prefix(prefix), items=self.items, next=self.next_page)

    def make_page(self, request: ReqT, response: httpx.Response) -> Page[ReqT, RespT, ItemT]:
        """Decode a response into a [`Page`][nanosdk.Page].

        Args:
            request: The request model that produced `response`.
            response: A successful response.

        Returns:
            The page, with its items extracted.
        """
        data = self.route.parse(response)
        return Page(request, data, list(self.items(data)), response)

    def __repr__(self) -> str:
        return f"<paginated {self.route!r}>"

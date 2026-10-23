from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any, Self, cast, overload
from urllib.parse import quote

import httpx
from pydantic import BaseModel, RootModel, TypeAdapter, ValidationError

from ._errors import ResponseValidationError
from ._paginate import NextPage, Page
from ._params import Location, Param

if TYPE_CHECKING:
    from ._client import (
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
    """Everything needed to build an ``httpx.Request`` for one call."""

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
    """One HTTP endpoint: method + path + request model + response type.

    ``P`` is the request model's constructor signature, so calling the bound route
    is typed exactly like constructing the model.
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

    # -- helpers ------------------------------------------------------------

    def _clone(self) -> Self:
        clone = type(self).__new__(type(self))
        clone.__dict__.update(
            {k: v for k, v in self.__dict__.items() if k not in ("_plan", "_adapter")}
        )
        return clone

    def with_prefix(self, prefix: str) -> Self:
        clone = self._clone()
        clone.path = prefix.rstrip("/") + "/" + self.path.lstrip("/") if self.path else prefix
        return clone

    def map[U](self, fn: Callable[[RespT], U]) -> Route[P, ReqT, U]:
        """A copy whose decoded response is passed through ``fn``: ``.map(frames.polars)``."""
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

        ``next`` picks the next request (``Cursor``, ``Offset``, ``PageNumber``,
        ``LinkHeader`` or your own function of the ``Page``). ``items`` pulls the item
        list out of a page; it can be omitted when the response is already a list.
        """
        return PaginatedRoute(self, items=items or _as_items, next=next)

    def __repr__(self) -> str:
        req = getattr(self.request, "__name__", None)
        resp = getattr(self.response, "__name__", repr(self.response))
        return f"<Route {self.name or '?'}: {self.method} {self.path} ({req}) -> {resp}>"

    def build_model(self, *args: P.args, **kwargs: P.kwargs) -> ReqT:
        """Construct the request model from call arguments."""
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
        """Turn a request model instance into URL / params / headers / body."""
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
        """Decode a successful ``httpx.Response`` into the declared type, then apply ``map``s."""
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


class _Verb:
    """``nanoapi.get(...)`` etc. Overloads give the right ``Route[P, Req, Resp]``."""

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
        return Route(
            self.method,
            path,
            request=request,
            response=response,
            headers=headers,
            exclude_unset=exclude_unset,
            meta=meta,
        )


get = _Verb("GET")
post = _Verb("POST")
put = _Verb("PUT")
patch = _Verb("PATCH")
delete = _Verb("DELETE")
head = _Verb("HEAD")
options = _Verb("OPTIONS")


def route(method: str) -> _Verb:
    """Any other HTTP method: ``nanoapi.route("PROPFIND")("/x", response=...)``."""
    return _Verb(method.upper())


class Routes:
    """Base for a reusable group of routes. Mix it into ``Client`` / ``AsyncClient``.

    ``class Users(nanoapi.Routes, prefix="/users"): ...`` prefixes every route path.
    """

    def __init_subclass__(cls, *, prefix: str = "", **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if prefix:
            for attr, value in list(vars(cls).items()):
                if isinstance(value, Route | PaginatedRoute):
                    setattr(cls, attr, value.with_prefix(prefix))


class PaginatedRoute[**P, ReqT, RespT, ItemT]:
    """A route plus the rules for walking its pages. Made by ``Route.paginate``.

    Bound to a client it gains ``.iter(...)`` (items across all pages) and
    ``.pages(...)`` (``Page`` objects); calling it still fetches just the first page.
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

    def with_prefix(self, prefix: str) -> Self:
        return type(self)(self.route.with_prefix(prefix), items=self.items, next=self.next_page)

    def make_page(self, request: ReqT, response: httpx.Response) -> Page[ReqT, RespT, ItemT]:
        data = self.route.parse(response)
        return Page(request, data, list(self.items(data)), response)

    def __repr__(self) -> str:
        return f"<paginated {self.route!r}>"

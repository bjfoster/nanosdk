"""Pagination: a ``Page`` record and the built-in next-page strategies.

A strategy is any callable ``(Page) -> next request``. It returns one of:

* a new request model -> sent through the route as usual
* an ``httpx.URL`` -> the same request re-sent to that URL (its query replaces the model's)
* ``None`` -> stop
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import httpx
from pydantic import BaseModel

__all__ = ["Cursor", "LinkHeader", "NextPage", "Offset", "Page", "PageNumber"]


@dataclass(frozen=True, slots=True)
class Page[ReqT, RespT, ItemT]:
    """One fetched page: the request model that produced it, the decoded body and its items."""

    request: ReqT
    data: RespT
    items: list[ItemT]
    response: httpx.Response


type NextPage[ReqT, RespT, ItemT] = Callable[[Page[ReqT, RespT, ItemT]], ReqT | httpx.URL | None]


@dataclass(frozen=True, slots=True)
class Cursor:
    """Read a cursor from the response and send it back in a request field.

    ``field`` is a dotted path into the decoded response (attributes or dict keys), or a
    function of it. Stops when the cursor is missing or empty.
    """

    field: str | Callable[[Any], Any]
    param: str = "cursor"

    def __call__(self, page: Page[Any, Any, Any]) -> Any:
        value = self.field(page.data) if callable(self.field) else _lookup(page.data, self.field)
        if value is None or value == "":
            return None
        return _with(page.request, self.param, value)


@dataclass(frozen=True, slots=True)
class Offset:
    """Advance the ``param`` request field by the number of items received.

    Stops on an empty page, or on a short one if ``limit`` names the page-size field.
    """

    param: str = "offset"
    limit: str | None = "limit"

    def __call__(self, page: Page[Any, Any, Any]) -> Any:
        if _is_last(page, self.limit):
            return None
        current = getattr(page.request, self.param, None) or 0
        return _with(page.request, self.param, current + len(page.items))


@dataclass(frozen=True, slots=True)
class PageNumber:
    """Increment the ``param`` request field (counting from ``start``).

    Stops on an empty page, or on a short one if ``size`` names the page-size field.
    """

    param: str = "page"
    start: int = 1
    size: str | None = None

    def __call__(self, page: Page[Any, Any, Any]) -> Any:
        if _is_last(page, self.size):
            return None
        current = getattr(page.request, self.param, None)
        return _with(page.request, self.param, (self.start if current is None else current) + 1)


@dataclass(frozen=True, slots=True)
class LinkHeader:
    """Follow the RFC 8288 ``Link: <...>; rel="next"`` response header (GitHub style)."""

    rel: str = "next"

    def __call__(self, page: Page[Any, Any, Any]) -> httpx.URL | None:
        url = page.response.links.get(self.rel, {}).get("url")
        return page.response.url.join(url) if url else None


def _lookup(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, Mapping):
            obj = cast(Mapping[str, Any], obj).get(part)
        else:
            obj = getattr(obj, part, None)
    return obj


def _is_last(page: Page[Any, Any, Any], size_field: str | None) -> bool:
    if not page.items:
        return True
    size = getattr(page.request, size_field, None) if size_field else None
    return isinstance(size, int) and len(page.items) < size


def _with(request: Any, param: str, value: Any) -> BaseModel:
    if not isinstance(request, BaseModel):
        raise TypeError(f"paginating by request field {param!r} needs a request model")
    if param not in type(request).model_fields:
        raise TypeError(f"{type(request).__name__} has no field {param!r} to paginate with")
    return request.model_copy(update={param: value})

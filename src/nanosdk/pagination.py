"""Pagination: the `Page` record and the built-in next-page strategies.

Make a route iterable with [`Route.paginate`][nanosdk.Route.paginate], passing a
strategy that picks the next request from the current page:

```python
list_posts = nanosdk.get("/posts", request=ListPosts, response=list[Post]).paginate(
    nanosdk.Offset("offset", limit="limit")
)
for post in api.list_posts.iter(limit=50): ...
```

A strategy is any callable `(Page) -> next`, where `next` is one of:

- a new request model: sent through the route as usual;
- an `httpx.URL`: the same request re-sent to that URL, whose query string replaces the
  model's query fields (for APIs that hand you a "next" link);
- `None`: stop.

Strategies that change a request field refer to it by its Python field name and need
the route to have a request model.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import httpx
from pydantic import BaseModel

__all__ = ["Cursor", "LinkHeader", "NextPage", "Offset", "Page", "PageNumber"]


@dataclass(frozen=True, slots=True)
class Page[ReqT, RespT, ItemT]:
    """One fetched page, as passed to strategies and yielded by `.pages()`.

    Attributes:
        request: The request model that produced this page (`None` for routes without one).
        data: The decoded response body, after any [`map`][nanosdk.Route.map].
        items: The page's items, as extracted by the route's `items` function.
        response: The raw `httpx.Response`, for headers such as `Link` or rate limits.
    """

    request: ReqT
    data: RespT
    items: list[ItemT]
    response: httpx.Response


type NextPage[ReqT, RespT, ItemT] = Callable[[Page[ReqT, RespT, ItemT]], ReqT | httpx.URL | None]
"""A pagination strategy: given the current page, the next request model, URL or `None`."""


@dataclass(frozen=True, slots=True)
class Cursor:
    """Read a cursor from each response and send it back in a request field.

    Stops when the cursor is missing, `None` or empty.

    Example:
        ```python
        # {"data": [...], "meta": {"next_cursor": "abc"}}  ->  ?cursor=abc
        nanosdk.Cursor("meta.next_cursor", param="cursor")
        nanosdk.Cursor(lambda body: body.paging.next, param="after")
        ```

    Args:
        field: Where the cursor is in the decoded response: a dotted path through
            attributes or dict keys, or a function of the response.
        param: The request-model field to put the cursor in.
    """

    field: str | Callable[[Any], Any]
    param: str = "cursor"

    def __call__(self, page: Page[Any, Any, Any]) -> Any:
        """Return the request for the page after `page`, or `None` when done."""
        value = self.field(page.data) if callable(self.field) else _lookup(page.data, self.field)
        if value is None or value == "":
            return None
        return _with(page.request, self.param, value)


@dataclass(frozen=True, slots=True)
class Offset:
    """Advance an offset request field by the number of items received.

    Stops on an empty page, or on one shorter than the page size.

    Example:
        ```python
        # ?offset=0&limit=50, ?offset=50&limit=50, ...
        nanosdk.Offset("offset", limit="limit")
        ```

    Args:
        param: The request-model field holding the offset (missing or `None` means 0).
        limit: The request-model field holding the page size, used to detect a short
            last page; `None` to rely on empty pages only.
    """

    param: str = "offset"
    limit: str | None = "limit"

    def __call__(self, page: Page[Any, Any, Any]) -> Any:
        """Return the request for the page after `page`, or `None` when done."""
        if _is_last(page, self.limit):
            return None
        current = getattr(page.request, self.param, None) or 0
        return _with(page.request, self.param, current + len(page.items))


@dataclass(frozen=True, slots=True)
class PageNumber:
    """Increment a page-number request field.

    Stops on an empty page, or on one shorter than the page size.

    Example:
        ```python
        # ?page=1&per_page=100, ?page=2&per_page=100, ...
        nanosdk.PageNumber("page", size="per_page")
        ```

    Args:
        param: The request-model field holding the page number.
        start: The number of the first page, assumed when the field is unset or `None`.
        size: The request-model field holding the page size, used to detect a short
            last page; `None` to rely on empty pages only.
    """

    param: str = "page"
    start: int = 1
    size: str | None = None

    def __call__(self, page: Page[Any, Any, Any]) -> Any:
        """Return the request for the page after `page`, or `None` when done."""
        if _is_last(page, self.size):
            return None
        current = getattr(page.request, self.param, None)
        return _with(page.request, self.param, (self.start if current is None else current) + 1)


@dataclass(frozen=True, slots=True)
class LinkHeader:
    """Follow the `Link: <url>; rel="next"` response header (RFC 8288).

    Used by GitHub, GitLab and many others. The linked URL (resolved against the
    response URL) replaces the query string; the route needs no request model.

    Args:
        rel: The link relation to follow.
    """

    rel: str = "next"

    def __call__(self, page: Page[Any, Any, Any]) -> httpx.URL | None:
        """Return the URL of the page after `page`, or `None` when there's no link."""
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

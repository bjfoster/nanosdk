"""Every pagination style, against an in-process fake API (no network).

Run:  python examples/pagination.py
"""

import asyncio
import json
from collections.abc import Callable, Iterator
from itertools import islice
from typing import Annotated, Any

import httpx
from pydantic import BaseModel

import nanosdk

# --- a fake API that paginates 23 items five different ways --------------------

ITEMS: list[dict[str, Any]] = [{"id": i, "name": f"item-{i:02}"} for i in range(1, 24)]


def fake_api(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    match request.url.path:
        case "/offset":  # ?offset=&limit=
            start, size = int(q.get("offset", 0)), int(q.get("limit", 10))
            return httpx.Response(200, json=ITEMS[start : start + size])
        case "/pages":  # ?page=&per_page=  -> {"results": [...], "page": n}
            page, size = int(q.get("page", 1)), int(q.get("per_page", 10))
            body = {"results": ITEMS[(page - 1) * size : page * size], "page": page}
            return httpx.Response(200, json=body)
        case "/cursor":  # ?after=  -> {"data": [...], "next_cursor": "..."}
            start = int(q.get("after", 0))
            chunk = ITEMS[start : start + 10]
            nxt = str(start + 10) if start + 10 < len(ITEMS) else None
            return httpx.Response(200, json={"data": chunk, "next_cursor": nxt})
        case "/stripe":  # ?starting_after=<id> -> {"data": [...], "has_more": bool}
            after = int(q.get("starting_after", 0))
            chunk = [item for item in ITEMS if item["id"] > after][:10]
            has_more = bool(chunk) and chunk[-1]["id"] < ITEMS[-1]["id"]
            return httpx.Response(200, json={"data": chunk, "has_more": has_more})
        case "/linked":  # Link: </linked?page=2>; rel="next"
            page = int(q.get("page", 1))
            headers = {"Link": f'</linked?page={page + 1}>; rel="next"'} if page * 10 < 23 else {}
            return httpx.Response(200, json=ITEMS[(page - 1) * 10 : page * 10], headers=headers)
        case _:
            return httpx.Response(404)


# --- models ---------------------------------------------------------------------


class Item(BaseModel):
    id: int
    name: str


class OffsetQuery(BaseModel):
    offset: int = 0
    limit: int = 10


class PageQuery(BaseModel):
    page: int = 1
    per_page: int = 10


class PageBody(BaseModel):
    results: list[Item]
    page: int


class CursorQuery(BaseModel):
    after: str | None = None


class CursorBody(BaseModel):
    data: list[Item]
    next_cursor: str | None


class StripeQuery(BaseModel):
    starting_after: Annotated[int | None, nanosdk.Query()] = None


class StripeBody(BaseModel):
    data: list[Item]
    has_more: bool


# --- a hand-written strategy: any function (Page) -> next request | URL | None ---


def stripe_next(page: nanosdk.Page[StripeQuery, StripeBody, Item]) -> StripeQuery | None:
    """Stripe style: ask for items after the last id we saw, while has_more."""
    if not page.data.has_more:
        return None
    return page.request.model_copy(update={"starting_after": page.items[-1].id})


# --- routes -----------------------------------------------------------------------


class Catalog(nanosdk.Routes):
    # Offset: advance by len(items); stop on a page shorter than `limit`.
    by_offset = nanosdk.get("/offset", request=OffsetQuery, response=list[Item]).paginate(
        nanosdk.Offset("offset", limit="limit")
    )
    # Page number: page += 1; stop on an empty or short page.
    by_page = nanosdk.get("/pages", request=PageQuery, response=PageBody).paginate(
        nanosdk.PageNumber("page", size="per_page"), items=lambda body: body.results
    )
    # Cursor: read `next_cursor` from the body, send it back as `after`.
    by_cursor = nanosdk.get("/cursor", request=CursorQuery, response=CursorBody).paginate(
        nanosdk.Cursor("next_cursor", param="after"), items=lambda body: body.data
    )
    # Your own strategy.
    by_stripe = nanosdk.get("/stripe", request=StripeQuery, response=StripeBody).paginate(
        stripe_next, items=lambda body: body.data
    )
    # RFC 8288 Link headers (GitHub, GitLab, ...). No request model needed.
    by_link = nanosdk.get("/linked", response=list[Item]).paginate(nanosdk.LinkHeader())


class Api(Catalog, nanosdk.Client):
    pass


class AsyncApi(Catalog, nanosdk.AsyncClient):
    pass


def counting_transport() -> tuple[httpx.MockTransport, list[str]]:
    log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(f"{request.url.path}?{request.url.query.decode()}")
        return fake_api(request)

    return httpx.MockTransport(handler), log


def main() -> None:
    transport, log = counting_transport()
    api = Api("https://catalog.test", transport=transport)

    styles: dict[str, Callable[[], Iterator[Item]]] = {
        "offset": api.by_offset.iter,
        "page": api.by_page.iter,
        "cursor": api.by_cursor.iter,
        "stripe": api.by_stripe.iter,
        "link": api.by_link.iter,
    }
    for name, iterate in styles.items():
        log.clear()
        ids = [item.id for item in iterate()]
        print(f"{name:<7} {len(ids)} items in {len(log)} requests: {' '.join(log)}")

    # Calling the route still returns only the first page, fully typed.
    first: PageBody = api.by_page(per_page=5)
    print(f"\nfirst page only: page={first.page}, {len(first.results)} results")

    # .pages() exposes each page's request, decoded body, items and raw response.
    for page in api.by_cursor.pages():
        print(
            f"  after={page.request.after!s:<5} -> {len(page.items)} items, "
            f"next_cursor={page.data.next_cursor}, status {page.response.status_code}"
        )

    # Lazy: islice(…, 3) makes exactly one request, however many pages exist.
    log.clear()
    print("\nfirst three:", [i.name for i in islice(api.by_offset.iter(limit=10), 3)], log)

    # Ad hoc: paginate a route that isn't declared on the class.
    adhoc = nanosdk.get("/offset", request=OffsetQuery, response=list[Item]).paginate(
        nanosdk.Offset()
    )
    print("ad hoc:", sum(1 for _ in adhoc.bind(api).iter(limit=7)), "items")

    # Async: the same routes give async iterators.
    async def run_async() -> None:
        async with AsyncApi("https://catalog.test", transport=counting_transport()[0]) as aapi:
            names = [item.name async for item in aapi.by_stripe.iter()]
            print(f"\nasync stripe-style: {len(names)} items, last {names[-1]}")
            sizes = [len(page.items) async for page in aapi.by_link.pages()]
            print("async link pages:", json.dumps(sizes))

    asyncio.run(run_async())


if __name__ == "__main__":
    main()

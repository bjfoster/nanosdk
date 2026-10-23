import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pandas as pd
import polars as pl
import pytest
from pydantic import BaseModel, RootModel, ValidationError

import nanoapi
from nanoapi.auth import ApiKey, Bearer, OAuth2ClientCredentials

type Handler = Callable[[httpx.Request], httpx.Response]


class Post(BaseModel):
    id: int
    title: str = ""


class Posts(RootModel[list[Post]]):
    pass


def transport(handler: Handler, log: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def wrapped(request: httpx.Request) -> httpx.Response:
        request.read()
        if log is not None:
            log.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped)


def posts(*ids: int) -> list[dict[str, Any]]:
    return [{"id": i, "title": f"t{i}"} for i in ids]


NO_WAIT = nanoapi.Retry(backoff=0, jitter=0)


# --- hooks & meta ---------------------------------------------------------------


class Hooked(nanoapi.Client):
    public = nanoapi.get("/public", response=list[Post], meta={"auth": False})
    private = nanoapi.get("/private", response=list[Post])

    def __init__(self, handler: Handler) -> None:
        super().__init__("https://api.test", transport=transport(handler))
        self.seen: list[str] = []

    def _prepare_request(
        self, request: httpx.Request, route: nanoapi.Route[Any, Any, Any]
    ) -> httpx.Request:
        if route.meta.get("auth", True):
            request.headers["X-Signed"] = "yes"
        return request

    def _process_response(
        self, response: httpx.Response, route: nanoapi.Route[Any, Any, Any]
    ) -> httpx.Response:
        self.seen.append(f"{route.name}:{response.status_code}")
        # unwrap an {"data": ...} envelope
        data = response.json()["data"]
        return httpx.Response(response.status_code, json=data, request=response.request)


def test_hooks_see_route_and_meta() -> None:
    signed: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        signed.append(request.headers.get("x-signed"))
        return httpx.Response(200, json={"data": posts(1)})

    api = Hooked(handler)
    assert api.public() == [Post(id=1, title="t1")]
    assert api.private()[0].id == 1
    assert signed == [None, "yes"]
    assert api.seen == ["public:200", "private:200"]


async def test_async_hooks() -> None:
    class Api(nanoapi.AsyncClient):
        list_posts = nanoapi.get("/posts", response=list[Post])

        async def _prepare_request(
            self, request: httpx.Request, route: nanoapi.Route[Any, Any, Any]
        ) -> httpx.Request:
            request.headers["X-Route"] = route.name or ""
            return request

    log: list[httpx.Request] = []
    api = Api("https://api.test", transport=transport(lambda r: httpx.Response(200, json=[]), log))
    assert await api.list_posts() == []
    assert log[0].headers["x-route"] == "list_posts"


# --- retry ----------------------------------------------------------------------


def flaky(*statuses: int, headers: dict[str, str] | None = None) -> Handler:
    remaining = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        if remaining:
            return httpx.Response(remaining.pop(0), headers=headers, json={"error": "busy"})
        return httpx.Response(200, json=posts(1))

    return handler


class RetryApi(nanoapi.Client):
    list_posts = nanoapi.get("/posts", response=list[Post])
    create = nanoapi.post("/posts", response=list[Post])
    fragile = nanoapi.get("/fragile", response=list[Post], meta={"retry": False})

    def _retry_policy(self, route: nanoapi.Route[Any, Any, Any]) -> nanoapi.Retry | None:
        return None if route.meta.get("retry") is False else super()._retry_policy(route)


def test_retries_transient_statuses() -> None:
    log: list[httpx.Request] = []
    api = RetryApi("https://api.test", retry=NO_WAIT, transport=transport(flaky(503, 429), log))
    assert api.list_posts()[0].id == 1
    assert len(log) == 3


def test_gives_up_after_max_retries() -> None:
    log: list[httpx.Request] = []
    api = RetryApi(
        "https://api.test", retry=NO_WAIT, transport=transport(flaky(503, 503, 503), log)
    )
    with pytest.raises(nanoapi.APIStatusError) as info:
        api.list_posts()
    assert info.value.status_code == 503
    assert len(log) == 3


def test_non_idempotent_and_opted_out_routes_are_not_retried() -> None:
    log: list[httpx.Request] = []
    api = RetryApi("https://api.test", retry=NO_WAIT, transport=transport(flaky(503, 503), log))
    with pytest.raises(nanoapi.APIStatusError):
        api.create()
    with pytest.raises(nanoapi.APIStatusError):
        api.fragile()
    assert [r.method for r in log] == ["POST", "GET"]


def test_with_options_can_disable_retry() -> None:
    log: list[httpx.Request] = []
    api = RetryApi("https://api.test", retry=NO_WAIT, transport=transport(flaky(503), log))
    with pytest.raises(nanoapi.APIStatusError):
        api.with_options(retry=None).list_posts()
    assert len(log) == 1


def test_retries_connection_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=posts(1))

    api = RetryApi("https://api.test", retry=NO_WAIT, transport=transport(handler))
    assert api.list_posts()[0].id == 1
    assert calls == 2


async def test_async_retry() -> None:
    class Api(nanoapi.AsyncClient):
        list_posts = nanoapi.get("/posts", response=list[Post])

    log: list[httpx.Request] = []
    api = Api("https://api.test", retry=NO_WAIT, transport=transport(flaky(502), log))
    assert (await api.list_posts())[0].id == 1
    assert len(log) == 2


def test_retry_delay() -> None:
    retry = nanoapi.Retry(backoff=1, jitter=0, max_delay=5)
    assert [retry.delay(n, None) for n in range(4)] == [1, 2, 4, 5]
    assert retry.delay(0, httpx.Response(429, headers={"Retry-After": "3"})) == 3
    assert retry.delay(0, httpx.Response(429, headers={"Retry-After": "99"})) == 5
    later = format_datetime(datetime.now(UTC) + timedelta(seconds=2), usegmt=True)
    assert 0 < retry.delay(0, httpx.Response(429, headers={"Retry-After": later})) <= 2
    jittery = nanoapi.Retry(backoff=1, jitter=0.5)
    assert all(0.5 <= jittery.delay(0, None) <= 1.5 for _ in range(50))


# --- map & frames ---------------------------------------------------------------


def test_map_and_frames() -> None:
    class Api(nanoapi.Client):
        count = nanoapi.get("/posts", response=list[Post]).map(len)
        frame = nanoapi.get("/posts", response=list[Post]).map(nanoapi.frames.polars)
        root_frame = nanoapi.get("/posts", response=Posts).map(nanoapi.frames.pandas)
        chained = nanoapi.get("/posts", response=list[Post]).map(len).map(str)

    api = Api(
        "https://api.test", transport=transport(lambda r: httpx.Response(200, json=posts(1, 2)))
    )
    assert api.count() == 2
    df = api.frame()
    assert isinstance(df, pl.DataFrame)
    assert df.columns == ["id", "title"]
    assert df["id"].to_list() == [1, 2]
    pdf = api.root_frame()
    assert isinstance(pdf, pd.DataFrame)
    assert list(pdf["title"]) == ["t1", "t2"]
    assert api.chained() == "2"


def test_records() -> None:
    class Nested(BaseModel):
        post: Post

    assert nanoapi.frames.records(Post(id=1)) == [{"id": 1, "title": ""}]
    assert nanoapi.frames.records({"a": 1}) == [{"a": 1}]
    assert nanoapi.frames.records([Nested(post=Post(id=2))]) == [{"post": {"id": 2, "title": ""}}]
    assert nanoapi.frames.polars([]).is_empty()
    with pytest.raises(TypeError, match="per row"):
        nanoapi.frames.records([1])  # type: ignore[list-item]  # pyright: ignore[reportArgumentType]


# --- pagination -----------------------------------------------------------------


class PostPage(BaseModel):
    data: list[Post]
    meta: dict[str, Any] = {}


class ByCursor(BaseModel):
    cursor: str | None = None


class ByOffset(BaseModel):
    offset: int = 0
    limit: int = 2


class ByPage(BaseModel):
    page: int | None = None


class Paged(nanoapi.Routes, prefix="/v1"):
    by_cursor = nanoapi.get("/cursor", request=ByCursor, response=PostPage).paginate(
        nanoapi.Cursor("meta.next"), items=lambda p: p.data
    )
    by_offset = nanoapi.get("/offset", request=ByOffset, response=list[Post]).paginate(
        nanoapi.Offset()
    )
    by_page = nanoapi.get("/page", request=ByPage, response=list[Post]).paginate(
        nanoapi.PageNumber()
    )
    by_link = nanoapi.get("/link", response=list[Post]).paginate(nanoapi.LinkHeader())
    stuck = nanoapi.get("/stuck", request=ByCursor, response=list[Post]).paginate(
        lambda page: page.request
    )


class PagedApi(Paged, nanoapi.Client):
    pass


class AsyncPagedApi(Paged, nanoapi.AsyncClient):
    pass


def pager(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    match request.url.path:
        case "/v1/cursor":
            pages: dict[str, tuple[list[dict[str, Any]], str | None]] = {
                "": (posts(1, 2), "c2"),
                "c2": (posts(3), "c3"),
                "c3": ([], None),
            }
            items, nxt = pages[q.get("cursor", "")]
            return httpx.Response(200, json={"data": items, "meta": {"next": nxt}})
        case "/v1/offset":
            everything = posts(1, 2, 3, 4, 5)
            start, limit = int(q["offset"]), int(q["limit"])
            return httpx.Response(200, json=everything[start : start + limit])
        case "/v1/page":
            page = int(q.get("page", "1"))
            return httpx.Response(200, json=posts(page) if page <= 3 else [])
        case "/v1/link":
            page = int(q.get("p", "1"))
            headers = {"Link": f'</v1/link?p={page + 1}>; rel="next"'} if page < 3 else {}
            return httpx.Response(200, json=posts(page), headers=headers)
        case "/v1/stuck":
            return httpx.Response(200, json=posts(1))
        case _:
            return httpx.Response(404)


@pytest.fixture
def log() -> list[httpx.Request]:
    return []


@pytest.fixture
def paged(log: list[httpx.Request]) -> PagedApi:
    return PagedApi("https://api.test", transport=transport(pager, log))


def ids(items: Any) -> list[int]:
    return [p.id for p in items]


def test_cursor(paged: PagedApi, log: list[httpx.Request]) -> None:
    assert ids(paged.by_cursor.iter()) == [1, 2, 3]
    assert [r.url.params.get("cursor") for r in log] == [None, "c2", "c3"]


def test_offset_stops_on_short_page(paged: PagedApi, log: list[httpx.Request]) -> None:
    assert ids(paged.by_offset.iter()) == [1, 2, 3, 4, 5]
    assert [r.url.params["offset"] for r in log] == ["0", "2", "4"]


def test_page_number_stops_on_empty_page(paged: PagedApi, log: list[httpx.Request]) -> None:
    assert ids(paged.by_page.iter()) == [1, 2, 3]
    assert [r.url.params.get("page") for r in log] == [None, "2", "3", "4"]


def test_link_header(paged: PagedApi, log: list[httpx.Request]) -> None:
    assert ids(paged.by_link.iter()) == [1, 2, 3]
    assert str(log[-1].url) == "https://api.test/v1/link?p=3"


def test_pages_and_first_page_call(paged: PagedApi) -> None:
    pages = list(paged.by_cursor.pages())
    assert [len(p.items) for p in pages] == [2, 1, 0]
    assert pages[1].request == ByCursor(cursor="c2")
    assert pages[0].data.meta == {"next": "c2"}
    assert pages[0].response.status_code == 200
    assert ids(paged.by_cursor().data) == [1, 2]  # plain call: first page only


def test_iteration_is_lazy_but_arguments_are_checked_upfront(
    paged: PagedApi, log: list[httpx.Request]
) -> None:
    it = paged.by_offset.iter(limit=1)
    assert log == []
    assert next(it).id == 1
    assert len(log) == 1
    with pytest.raises(ValidationError):  # before any request
        paged.by_offset.iter(limit="lots")  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


def test_stuck_pagination_stops(paged: PagedApi, log: list[httpx.Request]) -> None:
    assert ids(paged.stuck.iter()) == [1]
    assert len(log) == 1


def test_ad_hoc_pagination_and_prefix(paged: PagedApi) -> None:
    assert Paged.by_offset.route.path == "/v1/offset"
    assert Paged.by_offset.route.name == "by_offset"
    assert ids(Paged.by_page.bind(paged).iter(page=2)) == [2, 3]


def test_bad_pagination_field() -> None:
    route = nanoapi.get("/v1/offset", request=ByPage, response=list[Post]).paginate(
        nanoapi.Offset()
    )
    client = nanoapi.Client(
        "https://api.test", transport=transport(lambda r: httpx.Response(200, json=posts(1)))
    )
    with pytest.raises(TypeError, match="no field 'offset'"):
        list(route.bind(client).iter())


def test_pagination_errors_surface(log: list[httpx.Request]) -> None:
    api = PagedApi("https://api.test", transport=transport(lambda r: httpx.Response(500), log))
    with pytest.raises(nanoapi.APIStatusError):
        list(api.by_page.iter())


async def test_async_pagination(log: list[httpx.Request]) -> None:
    async with AsyncPagedApi("https://api.test", transport=transport(pager, log)) as api:
        assert ids([p async for p in api.by_cursor.iter()]) == [1, 2, 3]
        assert [len(p.items) async for p in api.by_link.pages()] == [1, 1, 1]
        assert ids((await api.by_cursor()).data) == [1, 2]


# --- auth -----------------------------------------------------------------------


def echo(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"headers": dict(request.headers), "url": str(request.url)})


class Echo(nanoapi.Client):
    whoami = nanoapi.get("/me", response=dict[str, Any])


def test_bearer_static_and_callable() -> None:
    api = Echo("https://api.test", auth=Bearer("t0k"), transport=transport(echo))
    assert api.whoami()["headers"]["authorization"] == "Bearer t0k"
    tokens = iter(["a", "b"])
    api = Echo("https://api.test", auth=Bearer(lambda: next(tokens)), transport=transport(echo))
    assert [api.whoami()["headers"]["authorization"] for _ in range(2)] == ["Bearer a", "Bearer b"]


def test_api_key() -> None:
    api = Echo("https://api.test", auth=ApiKey("X-Api-Key", "k"), transport=transport(echo))
    assert api.whoami()["headers"]["x-api-key"] == "k"
    api = Echo(
        "https://api.test", auth=ApiKey("key", "k", location="query"), transport=transport(echo)
    )
    assert api.whoami()["url"] == "https://api.test/me?key=k"


class TokenServer:
    def __init__(self) -> None:
        self.issued = 0
        self.valid: set[str] = set()
        self.token_requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            self.token_requests.append(request)
            self.issued += 1
            token = f"tok{self.issued}"
            self.valid = {token}
            return httpx.Response(200, json={"access_token": token, "expires_in": 3600})
        auth = request.headers.get("authorization", "").removeprefix("Bearer ")
        if auth not in self.valid:
            return httpx.Response(401)
        return httpx.Response(200, json={"token": auth})


def oauth(clock: Callable[[], float] = lambda: 0.0, **kwargs: Any) -> OAuth2ClientCredentials:
    return OAuth2ClientCredentials(
        "https://auth.test/oauth/token", "id", "secret", scope="read", clock=clock, **kwargs
    )


def test_oauth_fetches_caches_and_expires() -> None:
    server, now = TokenServer(), [0.0]
    api = Echo("https://api.test", auth=oauth(lambda: now[0]), transport=transport(server))
    assert api.whoami() == {"token": "tok1"}
    assert api.whoami() == {"token": "tok1"}
    assert server.issued == 1
    token_request = server.token_requests[0]
    assert token_request.headers["authorization"] == "Basic aWQ6c2VjcmV0"
    assert dict(httpx.QueryParams(token_request.content.decode())) == {
        "grant_type": "client_credentials",
        "scope": "read",
    }
    now[0] = 3600 - 29  # inside the 30s leeway
    assert api.whoami() == {"token": "tok2"}


def test_oauth_refreshes_on_401() -> None:
    server = TokenServer()
    api = Echo("https://api.test", auth=oauth(), transport=transport(server))
    api.whoami()
    server.valid = set()  # server revokes the token
    assert api.whoami() == {"token": "tok2"}


def test_oauth_credentials_in_body() -> None:
    server = TokenServer()
    api = Echo("https://api.test", auth=oauth(credentials_in="body"), transport=transport(server))
    api.whoami()
    form = dict(httpx.QueryParams(server.token_requests[0].content.decode()))
    assert form["client_id"] == "id"
    assert form["client_secret"] == "secret"
    assert "authorization" not in server.token_requests[0].headers


def test_oauth_token_errors() -> None:
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client"})

    api = Echo("https://api.test", auth=oauth(), transport=transport(deny))
    with pytest.raises(nanoapi.APIStatusError) as info:
        api.whoami()
    assert info.value.body == {"error": "invalid_client"}
    with pytest.raises(ValueError, match="absolute"):
        OAuth2ClientCredentials("/oauth/token", "id", "secret")


async def test_oauth_async() -> None:
    class AsyncEcho(nanoapi.AsyncClient):
        whoami = nanoapi.get("/me", response=dict[str, Any])

    server = TokenServer()
    api = AsyncEcho("https://api.test", auth=oauth(), transport=transport(server))
    assert await api.whoami() == {"token": "tok1"}
    assert await api.whoami() == {"token": "tok1"}
    assert server.issued == 1


def test_meta_is_copied_not_shared() -> None:
    meta = {"a": 1}
    route = nanoapi.get("/x", meta=meta)
    meta["a"] = 2
    assert route.meta == {"a": 1}
    assert json.dumps(route.map(lambda r: r).meta) == '{"a": 1}'

# nanoapi

A tiny, fully typed HTTP client core built on **httpx** + **pydantic**.
You declare routes and models; nanoapi gives you a sync *and* async client whose
methods are typed end to end — arguments come from the request model's
constructor, return values from the response type.

```python
from pydantic import BaseModel
import nanoapi

class User(BaseModel):
    id: int
    login: str

class GetUser(BaseModel):
    username: str

class GitHub(nanoapi.Client):
    get_user = nanoapi.get("/users/{username}", request=GetUser, response=User)

with GitHub("https://api.github.com") as gh:
    user = gh.get_user(username="octocat")   # -> User   (username: str is checked)
```

No codegen, no metaclass magic: a `Route` is a generic descriptor, and the
request model class is captured as a `Callable[P, Req]`, so `P` (its `__init__`
signature) becomes the signature of the bound method. Pyright and mypy (strict)
both see it.

## Install

```bash
uv sync                     # from this folder; installs the dev group (pytest, pyright, mypy, ty, ruff)
pip install -e ".[polars]"  # or without uv; extras: polars, pandas
```

Python 3.13+, httpx ≥ 0.27, pydantic ≥ 2.6.

## Declaring routes

```python
nanoapi.get(path, request=Model, response=Type, headers={...}, exclude_unset=None, meta={...})
```

`get / post / put / patch / delete / head / options`, plus `nanoapi.route("PROPFIND")` for anything else.

| `request=`            | call signature                |
|-----------------------|-------------------------------|
| omitted               | `api.thing()` — no arguments  |
| a `BaseModel` class   | `api.thing(**model fields)`   |
| a `RootModel` class   | `api.thing(root_value)` — whole body |

| `response=`                     | you get back           |
|---------------------------------|------------------------|
| omitted / `None`                | `None`                 |
| a model, `list[Model]`, `dict[str, int]`, … | validated via `TypeAdapter` |
| `str` / `bytes`                 | `response.text` / `.content` |
| `httpx.Response`                | the raw response       |

## Where fields go

Unmarked fields are placed by convention:

- name matches a `{placeholder}` in the path → **path** (URL-encoded)
- GET / DELETE / HEAD / OPTIONS → **query** (`None` dropped, lists repeated)
- POST / PUT / PATCH → **key in a JSON object body**

Override with `Annotated` markers:

```python
from typing import Annotated
from nanoapi import Path, Query, Header, Json, Body, Form, File

class UploadAvatar(BaseModel):
    user_id: Annotated[int, Path()]
    request_id: Annotated[str | None, Header()] = None          # -> request-id
    caption: Annotated[str, Form()]
    file: Annotated[tuple[str, bytes, str], File()]               # (name, bytes, content-type)

class ReplaceTags(BaseModel):
    user_id: int
    tags: Annotated[list[str], Body()]                            # the whole JSON body

class Search(BaseModel):
    q: Annotated[str, Query(alias="query")]
```

Pydantic aliases (`Field(alias=...)`, `serialization_alias`, `alias_generator`) are
respected on the wire. PATCH bodies send only fields you actually set
(`exclude_unset`), so `api.edit(post_id=1, title=None)` sends `{"title": null}`
and nothing else; pass `exclude_unset=` on the route to change that.

## Grouping and reuse

`nanoapi.Routes` is a mixin for groups of routes, with an optional prefix. Mix the
same group into a sync and an async client:

```python
class Users(nanoapi.Routes, prefix="/users"):
    get = nanoapi.get("/{user_id}", request=GetUser, response=User)
    list = nanoapi.get("", response=list[User])

class Api(Users, Billing, nanoapi.Client): ...
class AsyncApi(Users, Billing, nanoapi.AsyncClient): ...

user = Api(url).get(user_id=1)               # User
user = await AsyncApi(url).get(user_id=1)    # User
```

Or skip the subclass and hand routes to an instance as plain objects:

```python
get_user = nanoapi.get("/users/{user_id}", request=GetUser, response=User)
client = nanoapi.Client("https://api.example.com")
client.call(get_user, user_id=1)               # -> User
client.send(get_user, GetUser(user_id=1))      # -> User
```

## On a bound route

```python
api.get_user(user_id=1)                 # build model, send, check status, decode
api.get_user.send(GetUser(user_id=1))   # pre-built model
api.get_user.raw(user_id=1)             # httpx.Response, no status check / decode
api.get_user.build_request(user_id=1)   # httpx.Request, not sent
```

## Client options

```python
api = Api(
    "https://api.example.com/v2",
    headers={"User-Agent": "my-sdk/1.0"},
    auth=nanoapi.auth.Bearer(token),  # or ("user", "pass"), or any httpx.Auth
    timeout=10,
    retry=nanoapi.Retry(),            # retry 408/429/502/503/504 + connection errors
    error_model=ApiError,             # parse 4xx/5xx bodies into APIStatusError.error
    transport=httpx.HTTPTransport(retries=3),   # any other httpx.Client kwarg
)
api = Api(http_client=my_httpx_client)          # or bring your own (not closed for you)

scoped = api.with_options(headers={"Authorization": f"Bearer {tok}"}, timeout=60)
```

`with_options` returns a copy of the same class sharing the connection pool
(`with_options(retry=None)` turns retries off for that copy).

## Auth

`nanoapi.auth` has `httpx.Auth` implementations that work with sync and async clients:

```python
from nanoapi.auth import ApiKey, Bearer, OAuth2ClientCredentials

Bearer("tok")                          # Authorization: Bearer tok
Bearer(lambda: vault.read("token"))    # called per request
ApiKey("X-Api-Key", key)               # header; ApiKey("key", key, location="query")
OAuth2ClientCredentials(
    "https://auth.example.com/oauth/token", client_id, client_secret, scope="read",
)   # fetches + caches the token, refreshes before expiry and once on a 401
```

## Pagination

`.paginate(next)` makes a route iterable across pages. `next` picks the following
request from the current `Page` (`.request`, `.data`, `.items`, `.response`):

```python
class Posts(nanoapi.Routes, prefix="/posts"):
    # response is a list -> its items are the page's items
    by_offset = nanoapi.get("", request=ListPosts, response=list[Post]).paginate(
        nanoapi.Offset("offset", limit="limit")    # stops on an empty or short page
    )
    # response wraps the list -> say where the items are
    by_cursor = nanoapi.get("/feed", request=Feed, response=FeedPage).paginate(
        nanoapi.Cursor("meta.next_cursor", param="cursor"), items=lambda page: page.data
    )

api.by_offset(limit=50)              # list[Post]: just the first page, as before
api.by_offset.iter(limit=50)         # Iterator[Post] across every page, lazily
api.by_offset.pages(limit=50)        # Iterator[Page[ListPosts, list[Post], Post]]
async for post in aapi.by_cursor.iter(): ...   # AsyncIterator[Post]
client.iter(Posts.by_offset, limit=50)          # ad hoc, like client.call
```

Strategies: `Cursor`, `Offset`, `PageNumber("page", size="per_page")`,
`LinkHeader()` (RFC 8288 `rel="next"`, as GitHub uses), or any function
`(Page) -> next request model | httpx.URL | None`. Arguments are validated when you
call `.iter()`; requests go out as you consume it. A strategy that returns the same
request twice stops instead of looping forever.

## Transforming responses and DataFrames

`.map(fn)` runs `fn` on the decoded response; the route's return type follows:

```python
count = nanoapi.get("/posts", response=list[Post]).map(len)                    # -> int
frame = nanoapi.get("/posts", response=list[Post]).map(nanoapi.frames.polars)  # -> pl.DataFrame
```

`nanoapi.frames.polars` / `.pandas` (extras: `pip install "nanoapi[polars]"`) turn
models, dicts, or any iterable of them into a DataFrame, so they also work on
pagination: `nanoapi.frames.polars(api.by_offset.iter())`. `frames.records` gives
the plain-dict rows.

## Custom behaviour

Subclass the client and override any of these:

```python
class Api(nanoapi.Client):
    public = nanoapi.get("/status", response=Status, meta={"auth": False})

    def prepare_request(self, request, route):    # every attempt: sign, log, rewrite
        if route.meta.get("auth", True):
            request.headers["X-Signature"] = sign(request)
        return request

    def process_response(self, response, route):  # before status check + decoding
        return response

    def retry_policy(self, route):                # per-route retry, e.g. from meta
        return None if route.meta.get("no_retry") else super().retry_policy(route)

    def raise_for_status(self, response): ...     # map errors your own way
```

On `AsyncClient`, `prepare_request` and `process_response` are `async def`. `meta=`
is free-form data on any route for your hooks to read. `Retry` is a frozen dataclass
(`max_retries`, `statuses`, `methods`, `backoff`, `max_delay`, `jitter`,
`respect_retry_after`); subclass it and override `should_retry` / `delay` for more.
POST isn't retried unless you add it to `methods`.

## Errors

All inherit `nanoapi.NanoAPIError`:

- `APIStatusError` — 4xx/5xx; has `.status_code`, `.body` (JSON or text), `.error` (validated `error_model`), `.response`
- `APIConnectionError` / `APITimeoutError` — transport failures
- `ResponseValidationError` — a 2xx body didn't match `response=`

Invalid *arguments* raise pydantic's `ValidationError` before anything is sent.
Override `raise_for_status(self, response)` on your client to map errors your own way
(see [Custom behaviour](#custom-behaviour)).

## Development

```bash
uv run ruff check .      # lint
uv run ruff format .     # format
uv run pytest            # runtime tests (httpx.MockTransport, no network)
uv run pyright           # strict, incl. tests/typecheck.py assert_type + must-fail cases
uv run mypy              # strict
uv run ty check          # Astral's ty (3 of the 5 must-fail cases; see typecheck.py)
```

See [`examples/`](examples/README.md) for runnable SDKs against GitHub, PokéAPI and
jsonplaceholder, plus offline walkthroughs of pagination, auth, custom clients, request shapes,
DataFrames and testing.

# nanosdk

[![CI](https://github.com/bjfoster/nanosdk/actions/workflows/ci.yml/badge.svg)](https://github.com/bjfoster/nanosdk/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/nanosdk)](https://pypi.org/project/nanosdk/)
[![Python](https://img.shields.io/pypi/pyversions/nanosdk)](https://pypi.org/project/nanosdk/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

A tiny, fully typed HTTP client core built on **httpx** + **pydantic**.
You declare routes and models; nanosdk gives you a sync *and* async client whose
methods are typed end to end — arguments come from the request model's
constructor, return values from the response type.

```python
from pydantic import BaseModel
import nanosdk

class User(BaseModel):
    id: int
    login: str

class GetUser(BaseModel):
    username: str

class GitHub(nanosdk.Client):
    get_user = nanosdk.get("/users/{username}", request=GetUser, response=User)

with GitHub("https://api.github.com") as gh:
    user = gh.get_user(username="octocat")   # -> User   (username: str is checked)
```

No runtime codegen, no metaclass magic: a `Route` is a generic descriptor, and the
request model class is captured as a `Callable[P, Req]`, so `P` (its `__init__`
signature) becomes the signature of the bound method. pyright, mypy and ty (strict)
all see it. Have an OpenAPI spec? [`nanosdk generate`](#generating-a-client-from-openapi)
writes the routes and models for you.

## Install

```bash
pip install nanosdk                  # or: uv add nanosdk
pip install "nanosdk[codegen]"       # + `nanosdk generate` (OpenAPI -> typed client)
pip install "nanosdk[polars]"        # + DataFrame helpers (also: pandas)
```

Python 3.13+, httpx ≥ 0.27, pydantic ≥ 2.6.

<!-- --8<-- [start:guide] -->
## Declaring routes

```python
nanosdk.get(path, request=Model, response=Type, headers={...}, exclude_unset=None, meta={...})
```

`get / post / put / patch / delete / head / options`, plus `nanosdk.route("PROPFIND")` for anything else.

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
from nanosdk import Path, Query, Header, Json, Body, Form, File

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

`nanosdk.Routes` is a mixin for groups of routes, with an optional prefix. Mix the
same group into a sync and an async client:

```python
class Users(nanosdk.Routes, prefix="/users"):
    get = nanosdk.get("/{user_id}", request=GetUser, response=User)
    list = nanosdk.get("", response=list[User])

class Api(Users, Billing, nanosdk.Client): ...
class AsyncApi(Users, Billing, nanosdk.AsyncClient): ...

user = Api(url).get(user_id=1)               # User
user = await AsyncApi(url).get(user_id=1)    # User
```

Or skip the subclass and bind a route to any client:

```python
get_user = nanosdk.get("/users/{user_id}", request=GetUser, response=User)
client = nanosdk.Client("https://api.example.com")
get_user.bind(client)(user_id=1)               # -> User
get_user.bind(client).send(GetUser(user_id=1)) # -> User
```

### What's on a client instance

Your routes, plus `with_options()`, `close()` (`aclose()` on `AsyncClient`) and the
context-manager protocol: nothing else, so autocomplete on `api.` lists the API you
declared. nanosdk's own machinery is underscored, and declaring a route whose name
collides with one of the client's attributes (`close`, `with_options`, a hook, ...)
raises `TypeError` when the class is defined. For the underlying httpx client, use
`nanosdk.http_client(api)`.

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
    auth=nanosdk.auth.Bearer(token),  # or ("user", "pass"), or any httpx.Auth
    timeout=10,
    retry=nanosdk.Retry(),            # retry 408/429/502/503/504 + connection errors
    error_model=ApiError,             # parse 4xx/5xx bodies into APIStatusError.error
    transport=httpx.HTTPTransport(retries=3),   # any other httpx.Client kwarg
)
api = Api(http_client=my_httpx_client)          # or bring your own (not closed for you)

scoped = api.with_options(headers={"Authorization": f"Bearer {tok}"}, timeout=60)
```

`with_options` returns a copy of the same class sharing the connection pool
(`with_options(retry=None)` turns retries off for that copy).

## Auth

`nanosdk.auth` has `httpx.Auth` implementations that work with sync and async clients:

```python
from nanosdk.auth import ApiKey, Bearer, OAuth2ClientCredentials

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
class Posts(nanosdk.Routes, prefix="/posts"):
    # response is a list -> its items are the page's items
    by_offset = nanosdk.get("", request=ListPosts, response=list[Post]).paginate(
        nanosdk.Offset("offset", limit="limit")    # stops on an empty or short page
    )
    # response wraps the list -> say where the items are
    by_cursor = nanosdk.get("/feed", request=Feed, response=FeedPage).paginate(
        nanosdk.Cursor("meta.next_cursor", param="cursor"), items=lambda page: page.data
    )

api.by_offset(limit=50)              # list[Post]: just the first page, as before
api.by_offset.iter(limit=50)         # Iterator[Post] across every page, lazily
api.by_offset.pages(limit=50)        # Iterator[Page[ListPosts, list[Post], Post]]
async for post in aapi.by_cursor.iter(): ...   # AsyncIterator[Post]
Posts.by_offset.bind(client).iter(limit=50)    # a route not declared on the client
```

Strategies: `Cursor`, `Offset`, `PageNumber("page", size="per_page")`,
`LinkHeader()` (RFC 8288 `rel="next"`, as GitHub uses), or any function
`(Page) -> next request model | httpx.URL | None`. Arguments are validated when you
call `.iter()`; requests go out as you consume it. A strategy that returns the same
request twice stops instead of looping forever.

## Transforming responses and DataFrames

`.map(fn)` runs `fn` on the decoded response; the route's return type follows:

```python
count = nanosdk.get("/posts", response=list[Post]).map(len)                    # -> int
frame = nanosdk.get("/posts", response=list[Post]).map(nanosdk.frames.polars)  # -> pl.DataFrame
```

`nanosdk.frames.polars` / `.pandas` (extras: `pip install "nanosdk[polars]"`) turn
models, dicts, or any iterable of them into a DataFrame, so they also work on
pagination: `nanosdk.frames.polars(api.by_offset.iter())`. `frames.records` gives
the plain-dict rows.

## Custom behaviour

Subclass the client and override any of these hooks. They're underscored so they
stay out of the client's public namespace, next to your routes:

```python
class Api(nanosdk.Client):
    public = nanosdk.get("/status", response=Status, meta={"auth": False})

    def _prepare_request(self, request, route):    # every attempt: sign, log, rewrite
        if route.meta.get("auth", True):
            request.headers["X-Signature"] = sign(request)
        return request

    def _process_response(self, response, route):  # before status check + decoding
        return response

    def _retry_policy(self, route):                # per-route retry, e.g. from meta
        return None if route.meta.get("no_retry") else super()._retry_policy(route)

    def _raise_for_status(self, response): ...     # map errors your own way
```

On `AsyncClient`, `_prepare_request` and `_process_response` are `async def`. `meta=`
is free-form data on any route for your hooks to read. `Retry` is a frozen dataclass
(`max_retries`, `statuses`, `methods`, `backoff`, `max_delay`, `jitter`,
`respect_retry_after`); subclass it and override `should_retry` / `delay` for more.
POST isn't retried unless you add it to `methods`.

## Generating a client from OpenAPI

`nanosdk generate` turns an OpenAPI 3.x document into a typed client package: pydantic
models (via datamodel-codegen), a parameter model per operation, a `Routes` group per
tag, and sync and async clients. Everything stays typed, because it's ordinary
generated source that type checkers read:

```bash
pip install "nanosdk[codegen]"
nanosdk generate openapi.yml -o src/my_sdk/_generated --name MyApiBase
nanosdk generate openapi.yml -o src/my_sdk/_generated --name MyApiBase --check   # CI
```

```python
from my_sdk._generated import MyApiBase

api = MyApiBase()                          # base_url defaults to the spec's server
pet = api.get_pet_by_id(pet_id=1)          # -> Pet; arguments typed from the spec
```

- Parameters keep their wire names (`pet_id` is sent as `petId`), inline enums become
  `Literal[...]`, and `pattern` / `minimum` / ... are validated before sending.
- A JSON body model with no other parameters *is* the request: `add_pet(name=..., ...)`.
- Inline request/response schemas get stable names (`GetPetByIdResponse`), and route
  names come from `operationId` (or method + path), with `rename` to override them.
- Real-world spec quirks are handled: OpenAPI 3.0 `nullable` on required fields,
  `email`/`uri` formats as plain `str`, discriminated unions, GeoJSON schemas. For APIs
  whose responses break their own spec, `lenient` makes every model field optional.
- Treat the package as build output. Customise by subclassing the generated clients:
  add auth, hooks and conventions there, and `skip` operations whose parameters you'd
  rather write by hand (their models are still generated).

Settings can live in `pyproject.toml` (then it's just `nanosdk generate`):

```toml
[tool.nanosdk.codegen]
spec = "openapi.yml"
output = "src/my_sdk/_generated"
name = "MyApiBase"
transform = "scripts/fix_spec.py:fix"      # patch a broken spec in memory first
skip = ["updatePetWithForm"]
rename = { getPetById = "get_pet" }
lenient = false                            # true: every model field optional
```

Both `examples/petstore-workspace` and `examples/realtime-trains` in this repo are built
this way.

## Logging

nanosdk logs to the standard `logging` module under the `nanosdk` logger and prints
nothing until your application configures logging:

```python
import logging
logging.basicConfig(level=logging.WARNING)                 # retries only
logging.getLogger("nanosdk").setLevel(logging.DEBUG)       # every request too
```

| Level | Logger | What |
|---|---|---|
| DEBUG | `nanosdk` | every response: `GET https://api.example.com/users/1 -> 200 OK in 84 ms (get_user, attempt 1)`, and transport failures |
| DEBUG | `nanosdk` | each page fetched by `.iter()` / `.pages()` |
| WARNING | `nanosdk` | each retry: `Retrying GET ... in 1.03s after 503 Service Unavailable (get_user, retry 1 of 2)` |
| WARNING | `nanosdk` | pagination stopped because the strategy asked for the same page again |
| DEBUG / INFO | `nanosdk.auth` | OAuth token fetches; a token refused with 401 and replaced |

Records carry structured fields for JSON log formatters: `route`, `method`, `url`,
`attempt`, plus `status_code`, `elapsed_ms` or `retry_delay` where they apply.
URLs are logged as nanosdk built them, *before* auth is applied, and headers and bodies
are never logged, so tokens and API keys stay out of nanosdk's logs. (httpx itself logs
full URLs at INFO on the `httpx` logger, so prefer header API keys over query-string ones.)

An SDK can log under its own name: `Client(..., logger=logging.getLogger("my_sdk"))`.

## Errors

All inherit `nanosdk.NanoSDKError`:

- `APIStatusError` — 4xx/5xx; has `.status_code`, `.body` (JSON or text), `.error` (validated `error_model`), `.response`
- `APIConnectionError` / `APITimeoutError` — transport failures
- `ResponseValidationError` — a 2xx body didn't match `response=`

Invalid *arguments* raise pydantic's `ValidationError` before anything is sent.
Override `_raise_for_status(self, response)` on your client to map errors your own way
(see [Custom behaviour](#custom-behaviour)).

<!-- --8<-- [end:guide] -->

## Verifying a release

Every release from 0.2.0 on is built and published by GitHub Actions with provenance
anyone can check: SLSA build provenance on GitHub, and PEP 740 attestations on PyPI
(shown as "Provenance" on the PyPI file pages).

```bash
pip download nanosdk==0.2.0 --no-deps -d dist
gh attestation verify dist/nanosdk-0.2.0-py3-none-any.whl --repo bjfoster/nanosdk
```

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks and how releases work.

See [`examples/`](examples/README.md) for runnable SDKs against GitHub, PokéAPI and
jsonplaceholder, plus offline walkthroughs of pagination, auth, custom clients, request shapes,
DataFrames and testing.

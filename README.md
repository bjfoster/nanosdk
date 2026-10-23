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
pip install -e .            # or without uv
```

Python 3.13+, httpx ≥ 0.27, pydantic ≥ 2.6.

## Declaring routes

```python
nanoapi.get(path, request=Model, response=Type, headers={...}, exclude_unset=None)
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
    auth=("user", "pass"),            # or any httpx.Auth
    timeout=10,
    error_model=ApiError,             # parse 4xx/5xx bodies into APIStatusError.error
    transport=httpx.HTTPTransport(retries=3),   # any other httpx.Client kwarg
)
api = Api(http_client=my_httpx_client)          # or bring your own (not closed for you)

scoped = api.with_options(headers={"Authorization": f"Bearer {tok}"}, timeout=60)
```

`with_options` returns a copy of the same class sharing the connection pool.

## Errors

All inherit `nanoapi.NanoAPIError`:

- `APIStatusError` — 4xx/5xx; has `.status_code`, `.body` (JSON or text), `.error` (validated `error_model`), `.response`
- `APIConnectionError` / `APITimeoutError` — transport failures
- `ResponseValidationError` — a 2xx body didn't match `response=`

Invalid *arguments* raise pydantic's `ValidationError` before anything is sent.
Override `raise_for_status(self, response)` on your client to map errors your own way.

## Development

```bash
uv run ruff check .      # lint
uv run ruff format .     # format
uv run pytest            # runtime tests (httpx.MockTransport, no network)
uv run pyright           # strict, incl. tests/typecheck.py assert_type + must-fail cases
uv run mypy              # strict
uv run ty check          # Astral's ty (3 of the 5 must-fail cases; see typecheck.py)
```

See `examples/jsonplaceholder.py` for a full small SDK.

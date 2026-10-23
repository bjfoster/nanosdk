# nanosdk

A tiny, fully typed HTTP client core built on **httpx** and **pydantic**. Declare routes
with request and response models; nanosdk gives you sync *and* async clients whose methods
are typed end to end: arguments come from the request model's constructor, return values
from the response type.

```python
import nanosdk
from pydantic import BaseModel

class User(BaseModel):
    id: int
    login: str

class GetUser(BaseModel):
    username: str

class GitHub(nanosdk.Client):
    get_user = nanosdk.get("/users/{username}", request=GetUser, response=User)

with GitHub("https://api.github.com") as gh:
    user = gh.get_user(username="octocat")  # -> User; username: str is type-checked
```

No code generation and no metaclasses: a [`Route`][nanosdk.Route] is a generic descriptor,
and the request model's `__init__` signature becomes the signature of the bound method.
pyright, mypy and ty all see it.

## Install

```bash
pip install nanosdk                # or: uv add nanosdk
pip install "nanosdk[polars]"      # + DataFrame helpers (also: pandas)
```

Python 3.12+, httpx ≥ 0.27, pydantic ≥ 2.6.

## What's included

| | |
|---|---|
| **Routes** | [`get`][nanosdk.get], [`post`][nanosdk.post], ... with request fields placed in the path, query, headers, JSON, form or files ([field markers](reference/params.md)) |
| **Clients** | [`Client`][nanosdk.Client] and [`AsyncClient`][nanosdk.AsyncClient] sharing one set of route declarations |
| **Pagination** | [`Cursor`][nanosdk.Cursor], [`Offset`][nanosdk.Offset], [`PageNumber`][nanosdk.PageNumber], [`LinkHeader`][nanosdk.LinkHeader] or your own strategy; `.iter()` across pages |
| **Retries** | [`Retry`][nanosdk.Retry] with backoff, jitter and `Retry-After` |
| **Auth** | [`Bearer`][nanosdk.auth.Bearer], [`ApiKey`][nanosdk.auth.ApiKey], [`OAuth2ClientCredentials`][nanosdk.auth.OAuth2ClientCredentials], or any `httpx.Auth` |
| **Transforms** | [`Route.map`][nanosdk.Route.map], and [DataFrames](reference/frames.md) via polars or pandas |
| **Code generation** | [`nanosdk generate`](reference/codegen.md) writes a typed client package from an OpenAPI spec |
| **Hooks** | [`_prepare_request`][nanosdk.Client._prepare_request], [`_process_response`][nanosdk.Client._process_response], [`_raise_for_status`][nanosdk.Client._raise_for_status], [`_retry_policy`][nanosdk.Client._retry_policy]; a client instance exposes only your routes, `with_options` and `close` |

Read the [guide](guide.md) for a tour, or go straight to the [API reference](reference/index.md).

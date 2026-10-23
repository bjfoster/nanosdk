# nanoapi

A tiny, fully typed HTTP client core built on **httpx** and **pydantic**. Declare routes
with request and response models; nanoapi gives you sync *and* async clients whose methods
are typed end to end: arguments come from the request model's constructor, return values
from the response type.

```python
import nanoapi
from pydantic import BaseModel

class User(BaseModel):
    id: int
    login: str

class GetUser(BaseModel):
    username: str

class GitHub(nanoapi.Client):
    get_user = nanoapi.get("/users/{username}", request=GetUser, response=User)

with GitHub("https://api.github.com") as gh:
    user = gh.get_user(username="octocat")  # -> User; username: str is type-checked
```

No code generation and no metaclasses: a [`Route`][nanoapi.Route] is a generic descriptor,
and the request model's `__init__` signature becomes the signature of the bound method.
pyright, mypy and ty all see it.

## Install

```bash
pip install nanoapi                # or: uv add nanoapi
pip install "nanoapi[polars]"      # + DataFrame helpers (also: pandas)
```

Python 3.13+, httpx ≥ 0.27, pydantic ≥ 2.6.

## What's included

| | |
|---|---|
| **Routes** | [`get`][nanoapi.get], [`post`][nanoapi.post], ... with request fields placed in the path, query, headers, JSON, form or files ([field markers](reference/params.md)) |
| **Clients** | [`Client`][nanoapi.Client] and [`AsyncClient`][nanoapi.AsyncClient] sharing one set of route declarations |
| **Pagination** | [`Cursor`][nanoapi.Cursor], [`Offset`][nanoapi.Offset], [`PageNumber`][nanoapi.PageNumber], [`LinkHeader`][nanoapi.LinkHeader] or your own strategy; `.iter()` across pages |
| **Retries** | [`Retry`][nanoapi.Retry] with backoff, jitter and `Retry-After` |
| **Auth** | [`Bearer`][nanoapi.auth.Bearer], [`ApiKey`][nanoapi.auth.ApiKey], [`OAuth2ClientCredentials`][nanoapi.auth.OAuth2ClientCredentials], or any `httpx.Auth` |
| **Transforms** | [`Route.map`][nanoapi.Route.map], and [DataFrames](reference/frames.md) via polars or pandas |
| **Hooks** | [`_prepare_request`][nanoapi.Client._prepare_request], [`_process_response`][nanoapi.Client._process_response], [`_raise_for_status`][nanoapi.Client._raise_for_status], [`_retry_policy`][nanoapi.Client._retry_policy]; a client instance exposes only your routes, `with_options` and `close` |

Read the [guide](guide.md) for a tour, or go straight to the [API reference](reference/index.md).

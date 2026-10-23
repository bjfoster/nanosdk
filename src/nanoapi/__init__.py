"""nanoapi: a tiny, fully typed HTTP client core on httpx + pydantic.

Declare routes, get a typed client:

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
    user = gh.get_user(username="octocat")  # typed: User
```

Everything public is importable from `nanoapi` directly, plus the
[`nanoapi.auth`][nanoapi.auth] and [`nanoapi.frames`][nanoapi.frames] modules.
"""

from . import auth, frames
from .client import (
    NOT_GIVEN,
    AsyncBoundPaginatedRoute,
    AsyncBoundRoute,
    AsyncClient,
    BoundPaginatedRoute,
    BoundRoute,
    Client,
)
from .errors import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    NanoAPIError,
    ResponseValidationError,
)
from .pagination import Cursor, LinkHeader, NextPage, Offset, Page, PageNumber
from .params import Body, File, Form, Header, Json, Location, Param, Path, Query
from .retry import Retry
from .routing import (
    PaginatedRoute,
    Route,
    Routes,
    Verb,
    delete,
    get,
    head,
    options,
    patch,
    post,
    put,
    route,
)

__version__ = "0.1.0"

__all__ = [
    "NOT_GIVEN",
    "APIConnectionError",
    "APIStatusError",
    "APITimeoutError",
    "AsyncBoundPaginatedRoute",
    "AsyncBoundRoute",
    "AsyncClient",
    "Body",
    "BoundPaginatedRoute",
    "BoundRoute",
    "Client",
    "Cursor",
    "File",
    "Form",
    "Header",
    "Json",
    "LinkHeader",
    "Location",
    "NanoAPIError",
    "NextPage",
    "Offset",
    "Page",
    "PageNumber",
    "PaginatedRoute",
    "Param",
    "Path",
    "Query",
    "ResponseValidationError",
    "Retry",
    "Route",
    "Routes",
    "Verb",
    "auth",
    "delete",
    "frames",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "route",
]

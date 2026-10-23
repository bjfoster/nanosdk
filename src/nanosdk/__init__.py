"""nanosdk: a tiny, fully typed HTTP client core on httpx + pydantic.

Declare routes, get a typed client:

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
    user = gh.get_user(username="octocat")  # typed: User
```

Everything public is importable from `nanosdk` directly, plus the
[`nanosdk.auth`][nanosdk.auth] and [`nanosdk.frames`][nanosdk.frames] modules.
"""

import logging
from importlib.metadata import version as _version

from . import auth, frames
from .client import (
    NOT_GIVEN,
    AsyncBoundPaginatedRoute,
    AsyncBoundRoute,
    AsyncClient,
    BoundPaginatedRoute,
    BoundRoute,
    Client,
    http_client,
)
from .errors import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    NanoSDKError,
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

__version__ = _version("nanosdk")

# Libraries shouldn't print unless the application configures logging.
logging.getLogger("nanosdk").addHandler(logging.NullHandler())

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
    "NanoSDKError",
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
    "http_client",
    "options",
    "patch",
    "post",
    "put",
    "route",
]

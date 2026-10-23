"""nanoapi — a tiny, fully typed HTTP client core on httpx + pydantic.

Declare routes, get a typed client::

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
"""

from . import auth, frames
from ._client import (
    NOT_GIVEN,
    AsyncBoundPaginatedRoute,
    AsyncBoundRoute,
    AsyncClient,
    BoundPaginatedRoute,
    BoundRoute,
    Client,
)
from ._errors import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    NanoAPIError,
    ResponseValidationError,
)
from ._paginate import Cursor, LinkHeader, NextPage, Offset, Page, PageNumber
from ._params import Body, File, Form, Header, Json, Location, Param, Path, Query
from ._retry import Retry
from ._route import (
    PaginatedRoute,
    Route,
    Routes,
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

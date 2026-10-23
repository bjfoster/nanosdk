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

from ._client import NOT_GIVEN, AsyncBoundRoute, AsyncClient, BoundRoute, Client
from ._errors import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    NanoAPIError,
    ResponseValidationError,
)
from ._params import Body, File, Form, Header, Json, Location, Param, Path, Query
from ._route import Route, Routes, delete, get, head, options, patch, post, put, route

__version__ = "0.1.0"

__all__ = [
    "NOT_GIVEN",
    "APIConnectionError",
    "APIStatusError",
    "APITimeoutError",
    "AsyncBoundRoute",
    "AsyncClient",
    "Body",
    "BoundRoute",
    "Client",
    "File",
    "Form",
    "Header",
    "Json",
    "Location",
    "NanoAPIError",
    "Param",
    "Path",
    "Query",
    "ResponseValidationError",
    "Route",
    "Routes",
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "route",
]

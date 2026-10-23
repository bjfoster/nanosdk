"""Static typing checks. Not executed by pytest; run `pyright` / `mypy` over it."""

from typing import Annotated, assert_type

import httpx
from pydantic import BaseModel

import nanoapi


class User(BaseModel):
    id: int
    name: str


class GetUser(BaseModel):
    user_id: int
    include_posts: bool = False


class CreateUser(BaseModel):
    name: str
    trace_id: Annotated[str | None, nanoapi.Header()] = None


class UserRoutes(nanoapi.Routes, prefix="/users"):
    get = nanoapi.get("/{user_id}", request=GetUser, response=User)
    list = nanoapi.get("", response=list[User])
    create = nanoapi.post("", request=CreateUser, response=User)
    delete = nanoapi.delete("/{user_id}", request=GetUser)
    raw = nanoapi.get("/raw", response=httpx.Response)


class Api(UserRoutes, nanoapi.Client):
    ping = nanoapi.get("/ping", response=str)


class AsyncApi(UserRoutes, nanoapi.AsyncClient):
    pass


def sync_usage(api: Api) -> None:
    assert_type(api.get(user_id=1), User)
    assert_type(api.get(user_id=1, include_posts=True), User)
    assert_type(api.list(), "list[User]")
    assert_type(api.create(name="a", trace_id="t"), User)
    assert_type(api.delete(user_id=1), None)
    assert_type(api.ping(), str)
    assert_type(api.raw(), httpx.Response)
    assert_type(api.get.send(GetUser(user_id=1)), User)
    assert_type(api.get.raw(user_id=1), httpx.Response)
    assert_type(api.with_options(headers={"x": "y"}), Api)

    # ad-hoc: routes as plain objects, no subclass needed
    standalone = nanoapi.get("/users/{user_id}", request=GetUser, response=User)
    client = nanoapi.Client("https://example.com")
    assert_type(client.call(standalone, user_id=2), User)
    assert_type(client.send(standalone, GetUser(user_id=2)), User)

    # Errors the type checker must catch. ty models pydantic's lax coercion and
    # discarded extras, so it (rightly) accepts `user_id="nope"` and `bogus=1`.
    api.get(user_id="nope")  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    api.get()  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]  # ty: ignore[missing-argument]
    api.get(user_id=1, bogus=1)  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]
    api.list(1)  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]  # ty: ignore[too-many-positional-arguments]
    api.get.send(CreateUser(name="x"))  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]  # ty: ignore[invalid-argument-type]


async def async_usage(api: AsyncApi) -> None:
    assert_type(await api.get(user_id=1), User)
    assert_type(await api.list(), "list[User]")
    assert_type(await api.delete(user_id=1), None)
    assert_type(await api.get.raw(user_id=1), httpx.Response)

    standalone = nanoapi.get("/users/{user_id}", request=GetUser, response=User)
    assert_type(await api.call(standalone, user_id=2), User)


def class_access() -> None:
    assert_type(Api.ping, "nanoapi.Route[[], None, str]")

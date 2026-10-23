"""How to unit-test an SDK built on nanosdk: no mocking library, no network.

Hand the client an ``httpx.MockTransport`` and you control every response while
the real encoding, hooks, retries, pagination and decoding all still run.

Run:  pytest examples/test_my_sdk.py -q
"""

import json
from collections.abc import Callable, Iterator
from typing import Annotated

import httpx
import pytest
from pydantic import BaseModel

import nanosdk
from nanosdk.auth import Bearer

# --- the SDK under test (normally imported from your package) ---------------------


class Todo(BaseModel):
    id: int
    title: str
    done: bool = False


class NewTodo(BaseModel):
    title: str
    request_id: Annotated[str | None, nanosdk.Header()] = None


class ListTodos(BaseModel):
    page: int = 1
    done: bool | None = None


class TodoId(BaseModel):
    todo_id: int


class TodoApi(nanosdk.Client):
    list_todos = nanosdk.get("/todos", request=ListTodos, response=list[Todo]).paginate(
        nanosdk.PageNumber()
    )
    get_todo = nanosdk.get("/todos/{todo_id}", request=TodoId, response=Todo)
    create_todo = nanosdk.post("/todos", request=NewTodo, response=Todo)

    def __init__(
        self,
        token: str,
        *,
        retry: nanosdk.Retry | None = None,
        transport: httpx.BaseTransport | None = None,  # tests pass a MockTransport here
    ) -> None:
        super().__init__(
            "https://todo.test/v1", auth=Bearer(token), retry=retry, transport=transport
        )


# --- fixtures ----------------------------------------------------------------------

type Handler = Callable[[httpx.Request], httpx.Response]


class FakeServer:
    """Records requests and answers from a queue (or a function) you set per test."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response | Exception] = []
        self.handler: Handler | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def api(server: FakeServer) -> Iterator[TodoApi]:
    with TodoApi("t0k", transport=httpx.MockTransport(server)) as client:
        yield client


# --- tests ---------------------------------------------------------------------------


def test_get_decodes_and_sends_auth(api: TodoApi, server: FakeServer) -> None:
    server.responses.append(httpx.Response(200, json={"id": 3, "title": "write tests"}))

    todo = api.get_todo(todo_id=3)

    assert todo == Todo(id=3, title="write tests")
    assert server.last.url == "https://todo.test/v1/todos/3"
    assert server.last.headers["authorization"] == "Bearer t0k"


def test_create_sends_json_and_header(api: TodoApi, server: FakeServer) -> None:
    server.responses.append(httpx.Response(201, json={"id": 9, "title": "ship"}))

    api.create_todo(title="ship", request_id="r-1")

    assert server.last.method == "POST"
    assert json.loads(server.last.content) == {"title": "ship"}
    assert server.last.headers["request-id"] == "r-1"


def test_paginates_until_empty_page(api: TodoApi, server: FakeServer) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"id": page, "title": f"t{page}"}] if page < 4 else [])

    server.handler = handler

    assert [t.id for t in api.list_todos.iter(done=False)] == [1, 2, 3]
    assert [r.url.params["page"] for r in server.requests] == ["1", "2", "3", "4"]
    assert all(r.url.params["done"] == "false" for r in server.requests)


def test_build_request_needs_no_server(api: TodoApi) -> None:
    request = api.list_todos.build_request(page=2)
    assert str(request.url) == "https://todo.test/v1/todos?page=2"


def test_api_errors(api: TodoApi, server: FakeServer) -> None:
    server.responses.append(httpx.Response(404, json={"detail": "no such todo"}))

    with pytest.raises(nanosdk.APIStatusError) as info:
        api.get_todo(todo_id=404)

    assert info.value.status_code == 404
    assert info.value.body == {"detail": "no such todo"}


def test_unexpected_response_shape(api: TodoApi, server: FakeServer) -> None:
    server.responses.append(httpx.Response(200, json={"id": "not-a-number"}))
    with pytest.raises(nanosdk.ResponseValidationError):
        api.get_todo(todo_id=1)


def test_network_failure(api: TodoApi, server: FakeServer) -> None:
    server.responses.append(httpx.ConnectTimeout("slow"))
    with pytest.raises(nanosdk.APITimeoutError):
        api.get_todo(todo_id=1)


def test_retry_policy(server: FakeServer) -> None:
    server.responses += [httpx.Response(503), httpx.Response(200, json={"id": 1, "title": "x"})]
    retrying = TodoApi(
        "t0k", transport=httpx.MockTransport(server), retry=nanosdk.Retry(backoff=0, jitter=0)
    )

    assert retrying.get_todo(todo_id=1).id == 1
    assert len(server.requests) == 2

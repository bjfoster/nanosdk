import json
from typing import Annotated, Any

import httpx
import pytest
from pydantic import BaseModel, Field, RootModel, ValidationError

import nanosdk

# --- models -----------------------------------------------------------------


class User(BaseModel):
    id: int
    name: str
    email: str | None = None


class GetUser(BaseModel):
    user_id: int
    expand: list[str] | None = None
    verbose: bool = False


class CreateUser(BaseModel):
    name: str
    email: str | None = None
    idempotency_key: Annotated[str | None, nanosdk.Header()] = None


class UpdateUser(BaseModel):
    user_id: int
    name: str | None = None
    email: str | None = None


class Search(BaseModel):
    q: Annotated[str, nanosdk.Query(alias="query")]
    page_size: int = Field(default=10, alias="pageSize")


class Tags(RootModel[list[str]]):
    pass


class ReplaceTags(BaseModel):
    user_id: Annotated[int, nanosdk.Path()]
    tags: Annotated[list[str], nanosdk.Body()]


class Upload(BaseModel):
    user_id: int
    caption: Annotated[str, nanosdk.Form()]
    file: Annotated[tuple[str, bytes, str], nanosdk.File()]


class ApiError(BaseModel):
    code: str
    message: str


class UserRoutes(nanosdk.Routes, prefix="/users"):
    get_user = nanosdk.get("/{user_id}", request=GetUser, response=User)
    list_users = nanosdk.get("", response=list[User])
    create_user = nanosdk.post("", request=CreateUser, response=User)
    update_user = nanosdk.patch("/{user_id}", request=UpdateUser, response=User)
    delete_user = nanosdk.delete("/{user_id}", request=GetUser)
    replace_tags = nanosdk.put("/{user_id}/tags", request=ReplaceTags, response=list[str])
    upload = nanosdk.post("/{user_id}/avatar", request=Upload)


class Api(UserRoutes, nanosdk.Client):
    search = nanosdk.get("/search", request=Search, response=list[User])
    set_tags = nanosdk.post("/tags", request=Tags, response=Tags)
    health = nanosdk.get("/health", response=str, headers={"X-Probe": "1"})


class AsyncApi(UserRoutes, nanosdk.AsyncClient):
    pass


# --- fake server ------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.reply: Any = {"id": 1, "name": "ada"}
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        if isinstance(self.reply, str):
            return httpx.Response(self.status, text=self.reply)
        if self.reply is None:
            return httpx.Response(self.status)
        return httpx.Response(self.status, json=self.reply)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


@pytest.fixture
def server() -> Recorder:
    return Recorder()


@pytest.fixture
def api(server: Recorder) -> Api:
    return Api("https://api.test/v1", transport=httpx.MockTransport(server), error_model=ApiError)


@pytest.fixture
def aapi(server: Recorder) -> AsyncApi:
    return AsyncApi("https://api.test/v1", transport=httpx.MockTransport(server))


# --- encoding ---------------------------------------------------------------


def test_path_and_query(api: Api, server: Recorder) -> None:
    user = api.get_user(user_id=7, expand=["posts", "likes"], verbose=True)
    assert user == User(id=1, name="ada")
    req = server.last
    assert req.method == "GET"
    assert req.url.path == "/v1/users/7"
    assert req.url.params.get_list("expand") == ["posts", "likes"]
    assert req.url.params["verbose"] == "true"


def test_none_query_values_are_dropped(api: Api, server: Recorder) -> None:
    api.get_user(user_id=1)
    assert "expand" not in server.last.url.params


def test_path_values_are_url_encoded(server: Recorder) -> None:
    class ByName(BaseModel):
        name: str

    route = nanosdk.get("/files/{name}", request=ByName, response=dict[str, Any])
    client = nanosdk.Client("https://api.test", transport=httpx.MockTransport(server))
    route.bind(client)(name="a b/c")
    assert server.last.url.raw_path == b"/files/a%20b%2Fc"


def test_json_body_and_header(api: Api, server: Recorder) -> None:
    api.create_user(name="ada", email="a@x.io", idempotency_key="k1")
    req = server.last
    assert req.method == "POST"
    assert req.url.path == "/v1/users"
    assert json.loads(req.content) == {"name": "ada", "email": "a@x.io"}
    assert req.headers["idempotency-key"] == "k1"


def test_patch_sends_only_set_fields(api: Api, server: Recorder) -> None:
    api.update_user(user_id=3, email=None)
    assert json.loads(server.last.content) == {"email": None}
    assert server.last.url.path == "/v1/users/3"


def test_aliases(api: Api, server: Recorder) -> None:
    server.reply = []
    assert api.search(q="hi", pageSize=5) == []
    assert dict(server.last.url.params) == {"query": "hi", "pageSize": "5"}


def test_whole_body_field(api: Api, server: Recorder) -> None:
    server.reply = ["a", "b"]
    assert api.replace_tags(user_id=2, tags=["a", "b"]) == ["a", "b"]
    assert server.last.url.path == "/v1/users/2/tags"
    assert json.loads(server.last.content) == ["a", "b"]


def test_root_model_request_and_response(api: Api, server: Recorder) -> None:
    server.reply = ["x"]
    out = api.set_tags(["x"])
    assert out == Tags(["x"])
    assert json.loads(server.last.content) == ["x"]


def test_form_and_file(api: Api, server: Recorder) -> None:
    server.reply = None
    api.upload(user_id=1, caption="me", file=("me.png", b"\x89PNG", "image/png"))
    req = server.last
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="caption"' in req.content
    assert b"me.png" in req.content


def test_route_static_headers_and_str_response(api: Api, server: Recorder) -> None:
    server.reply = "ok"
    assert api.health() == "ok"
    assert server.last.headers["x-probe"] == "1"


def test_no_content_response(api: Api, server: Recorder) -> None:
    server.reply, server.status = None, 204
    assert api.delete_user(user_id=9) is None
    assert server.last.method == "DELETE"


def test_list_response(api: Api, server: Recorder) -> None:
    server.reply = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    users = api.list_users()
    assert [u.id for u in users] == [1, 2]


# --- call styles --------------------------------------------------------------


def test_send_raw_and_build_request(api: Api, server: Recorder) -> None:
    assert api.get_user.send(GetUser(user_id=4)).id == 1
    raw = api.get_user.raw(user_id=4)
    assert isinstance(raw, httpx.Response)
    assert raw.json()["name"] == "ada"
    built = api.get_user.build_request(user_id=5)
    assert str(built.url) == "https://api.test/v1/users/5?verbose=false"


def test_with_options(api: Api, server: Recorder) -> None:
    scoped = api.with_options(headers={"Authorization": "Bearer t"}, timeout=3)
    scoped.get_user(user_id=1)
    assert server.last.headers["authorization"] == "Bearer t"
    assert server.last.extensions["timeout"]["read"] == 3
    api.get_user(user_id=1)
    assert "authorization" not in server.last.headers


def test_bring_your_own_httpx_client(server: Recorder) -> None:
    http = httpx.Client(base_url="https://own.test", transport=httpx.MockTransport(server))
    with Api(http_client=http) as api:
        api.get_user(user_id=1)
    assert str(server.last.url).startswith("https://own.test/users/1")
    assert not http.is_closed  # we don't close what we didn't open
    with pytest.raises(ValueError, match="http_client"):
        Api("https://x", http_client=http)


# --- errors ---------------------------------------------------------------


def test_status_error_with_error_model(api: Api, server: Recorder) -> None:
    server.reply, server.status = {"code": "nope", "message": "missing"}, 404
    with pytest.raises(nanosdk.APIStatusError) as info:
        api.get_user(user_id=1)
    err = info.value
    assert err.status_code == 404
    assert err.body == {"code": "nope", "message": "missing"}
    assert err.error == ApiError(code="nope", message="missing")


def test_raw_does_not_raise_on_status(api: Api, server: Recorder) -> None:
    server.status = 500
    assert api.get_user.raw(user_id=1).status_code == 500


def test_response_validation_error(api: Api, server: Recorder) -> None:
    server.reply = {"id": "not-an-int"}
    with pytest.raises(nanosdk.ResponseValidationError):
        api.get_user(user_id=1)


def test_connection_and_timeout_errors(api: Api, server: Recorder) -> None:
    server.reply = httpx.ConnectError("refused")
    with pytest.raises(nanosdk.APIConnectionError):
        api.list_users()
    server.reply = httpx.ReadTimeout("slow")
    with pytest.raises(nanosdk.APITimeoutError):
        api.list_users()


def test_request_validation_happens_before_sending(api: Api, server: Recorder) -> None:
    with pytest.raises(ValidationError):
        api.get_user(user_id="abc")  # type: ignore[arg-type]
    assert server.requests == []


def test_custom_raise_for_status(server: Recorder) -> None:
    class Teapot(Exception):
        pass

    class Strict(Api):
        def _raise_for_status(self, response: httpx.Response) -> None:
            if response.status_code == 418:
                raise Teapot
            super()._raise_for_status(response)

    server.status = 418
    with pytest.raises(Teapot):
        Strict("https://x", transport=httpx.MockTransport(server)).list_users()


# --- definition-time checks ----------------------------------------------------


def test_missing_path_field_is_reported() -> None:
    class Nothing(BaseModel):
        other: int

    route = nanosdk.get("/a/{id}", request=Nothing)
    with pytest.raises(TypeError, match="id"):
        route.encode(Nothing(other=1))


def test_prefix_and_repr() -> None:
    assert Api.get_user.path == "/users/{user_id}"
    assert Api.list_users.path == "/users"
    assert "get_user" in repr(Api.get_user)


# --- async -------------------------------------------------------------------


async def test_async_roundtrip(aapi: AsyncApi, server: Recorder) -> None:
    async with aapi:
        user = await aapi.get_user(user_id=5)
        assert user.name == "ada"
        server.reply = [{"id": 1, "name": "a"}]
        assert len(await aapi.list_users()) == 1
        raw = await aapi.get_user.raw(user_id=1)
        assert raw.status_code == 200
        server.reply = {"id": 3}
        route = nanosdk.get("/users/{user_id}", request=GetUser, response=dict[str, Any])
        assert await route.bind(aapi)(user_id=3) == {"id": 3}


async def test_async_status_error(aapi: AsyncApi, server: Recorder) -> None:
    server.status = 503
    with pytest.raises(nanosdk.APIStatusError):
        await aapi.list_users()


# --- the client's namespace ---------------------------------------------------------


def test_instances_only_expose_routes_and_lifecycle() -> None:
    public = {name for name in dir(Api) if not name.startswith("_")}
    routes = {name for name, v in vars(Api).items() if isinstance(v, nanosdk.Route)}
    routes |= {name for name, v in vars(UserRoutes).items() if isinstance(v, nanosdk.Route)}
    assert public - routes == {"close", "with_options"}


@pytest.mark.parametrize("name", ["close", "with_options", "_send", "_prepare_request"])
def test_routes_cannot_shadow_the_client(name: str) -> None:
    with pytest.raises(TypeError, match=f"can't be called {name!r}"):
        type("Bad", (nanosdk.Client,), {name: nanosdk.get("/x")})
    group = type("Group", (nanosdk.Routes,), {name: nanosdk.get("/x")})  # fine on its own
    with pytest.raises(TypeError, match="can't be called"):
        type("Bad", (group, nanosdk.AsyncClient), {})


def test_routes_named_like_old_client_methods_just_work(server: Recorder) -> None:
    """`send` and `pages` used to shadow client internals and break every other route."""

    class Messages(nanosdk.Client):
        send = nanosdk.post("/messages", response=dict[str, Any])
        pages = nanosdk.get("/pages", response=dict[str, Any])
        inbox = nanosdk.get("/inbox", response=dict[str, Any])

    api = Messages("https://api.test", transport=httpx.MockTransport(server))
    assert api.send() == api.pages() == api.inbox() == {"id": 1, "name": "ada"}
    assert [r.url.path for r in server.requests] == ["/messages", "/pages", "/inbox"]


def test_http_client_escape_hatch(api: Api) -> None:
    assert isinstance(nanosdk.http_client(api), httpx.Client)

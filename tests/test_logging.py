import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

import nanosdk
from nanosdk.auth import ApiKey, Bearer, OAuth2ClientCredentials

type Handler = Callable[[httpx.Request], httpx.Response]

SECRET = "s3cret-token-value"


class Item(BaseModel):
    id: int


class Page(BaseModel):
    page: int = 1


class Api(nanosdk.Client):
    get_items = nanosdk.get("/items", response=list[Item])
    paged = nanosdk.get("/paged", request=Page, response=list[Item]).paginate(nanosdk.PageNumber())
    stuck = nanosdk.get("/paged", request=Page, response=list[Item]).paginate(lambda p: p.request)


def api(handler: Handler, **kwargs: Any) -> Api:
    return Api("https://api.test", transport=httpx.MockTransport(handler), **kwargs)


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=[{"id": 1}])


def flaky(*statuses: int) -> Handler:
    remaining = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(remaining.pop(0)) if remaining else ok(request)

    return handler


NO_WAIT = nanosdk.Retry(backoff=0, jitter=0)


def records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == level]


def test_silent_by_default(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="nanosdk")
    api(ok).get_items()
    assert caplog.records == []


def test_library_logger_has_a_null_handler() -> None:
    handlers = logging.getLogger("nanosdk").handlers
    assert any(isinstance(h, logging.NullHandler) for h in handlers)


def test_debug_logs_each_response_with_structured_fields(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="nanosdk")
    api(ok).get_items()

    [record] = records(caplog, logging.DEBUG)
    assert record.name == "nanosdk"
    assert record.getMessage().startswith("GET https://api.test/items -> 200 OK in ")
    assert record.getMessage().endswith("(get_items, attempt 1)")
    fields = record.__dict__
    assert fields["route"] == "get_items"
    assert fields["method"] == "GET"
    assert fields["url"] == "https://api.test/items"
    assert fields["status_code"] == 200
    assert fields["attempt"] == 1
    assert fields["elapsed_ms"] >= 0


def test_retries_are_warnings(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="nanosdk")
    api(flaky(503, 429), retry=NO_WAIT).get_items()

    warnings = records(caplog, logging.WARNING)
    assert [r.getMessage() for r in warnings] == [
        "Retrying GET https://api.test/items in 0.00s after 503 Service Unavailable "
        "(get_items, retry 1 of 2)",
        "Retrying GET https://api.test/items in 0.00s after 429 Too Many Requests "
        "(get_items, retry 2 of 2)",
    ]
    assert [r.__dict__["status_code"] for r in warnings] == [503, 429]
    assert warnings[0].__dict__["retry_delay"] == 0


def test_transport_errors(caplog: pytest.LogCaptureFixture) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("refused")
        return ok(request)

    caplog.set_level(logging.DEBUG, logger="nanosdk")
    api(handler, retry=NO_WAIT).get_items()

    messages = [r.getMessage() for r in caplog.records]
    assert "failed in" in messages[0]
    assert messages[0].endswith("ConnectError: refused (get_items, attempt 1)")
    assert messages[1].startswith("Retrying GET https://api.test/items in 0.00s after ConnectError")
    assert "-> 200 OK" in messages[2]
    assert "attempt 2" in messages[2]


def test_credentials_never_reach_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    api(ok, auth=Bearer(SECRET)).get_items()
    api(ok, auth=ApiKey("api_key", SECRET, location="query")).get_items()
    api(flaky(503), retry=NO_WAIT, auth=Bearer(SECRET)).get_items()  # a retry warning too

    ours = [r for r in caplog.records if r.name.startswith("nanosdk")]
    assert len(ours) == 5  # 200, 200, then 503 + retry warning + 200
    for record in ours:
        assert SECRET not in record.getMessage()
        assert SECRET not in repr(record.__dict__)
    # httpx's own INFO logs show URLs *after* auth, so query-string keys do appear there.
    assert any(SECRET in r.getMessage() for r in caplog.records if r.name == "httpx")


def test_custom_logger(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="my_sdk")
    api(ok, logger=logging.getLogger("my_sdk")).get_items()
    assert [r.name for r in caplog.records] == ["my_sdk"]


def test_pagination(caplog: pytest.LogCaptureFixture) -> None:
    def pages(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params["page"])
        return httpx.Response(200, json=[{"id": number}] if number < 3 else [])

    caplog.set_level(logging.DEBUG, logger="nanosdk")
    assert [i.id for i in api(pages).paged.iter()] == [1, 2]
    page_logs = [r.getMessage() for r in caplog.records if "page" in r.__dict__]
    assert page_logs == [
        "Page 1 of paged has 1 item(s)",
        "Page 2 of paged has 1 item(s)",
        "Page 3 of paged has 0 item(s)",
    ]

    caplog.clear()
    list(api(ok).stuck.iter())
    [warning] = records(caplog, logging.WARNING)
    assert warning.getMessage() == (
        "Stopped paginating stuck after page 1: the strategy asked for the same page again"
    )


def test_oauth(caplog: pytest.LogCaptureFixture) -> None:
    issued = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal issued
        if request.url.path == "/token":
            issued += 1
            return httpx.Response(
                200, json={"access_token": f"{SECRET}-{issued}", "expires_in": 60}
            )
        if request.headers["authorization"] == f"Bearer {SECRET}-1" and issued == 1:
            return httpx.Response(401)  # first token gets revoked
        return ok(request)

    caplog.set_level(logging.DEBUG, logger="nanosdk")
    oauth = OAuth2ClientCredentials("https://auth.test/token", "id", "secret")
    api(handler, auth=oauth).get_items()

    auth_logs = [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "nanosdk.auth"]
    assert auth_logs == [
        ("DEBUG", "Fetching an OAuth token from https://auth.test/token (no token yet)"),
        ("DEBUG", "Got an OAuth token valid for 60 s"),
        ("INFO", "GET https://api.test/items was refused with 401; fetching a new OAuth token "
                 "and retrying once"),
        ("DEBUG", "Got an OAuth token valid for 60 s"),
    ]  # fmt: skip
    for record in caplog.records:
        assert SECRET not in record.getMessage()


async def test_async_client_logs_too(caplog: pytest.LogCaptureFixture) -> None:
    class AsyncApi(nanosdk.AsyncClient):
        get_items = nanosdk.get("/items", response=list[Item])

    caplog.set_level(logging.DEBUG, logger="nanosdk")
    async with AsyncApi(
        "https://api.test", transport=httpx.MockTransport(flaky(502)), retry=NO_WAIT
    ) as client:
        await client.get_items()
    assert [r.levelname for r in caplog.records] == ["DEBUG", "WARNING", "DEBUG"]

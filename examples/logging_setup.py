"""Seeing what nanosdk does: plain-text and JSON logging, offline.

Run:  python examples/logging_setup.py
"""

import json
import logging
import sys

import httpx
from pydantic import BaseModel

import nanosdk
from nanosdk.auth import Bearer

# --- a flaky fake API: 503 on the first call, then fine ---------------------------

calls = 0


def fake_api(request: httpx.Request) -> httpx.Response:
    global calls
    calls += 1
    if calls == 1:
        return httpx.Response(503)
    page = int(request.url.params.get("page", 1))
    return httpx.Response(200, json=[{"id": page}] if page <= 2 else [])


class Item(BaseModel):
    id: int


class Page(BaseModel):
    page: int = 1


class Api(nanosdk.Client):
    items = nanosdk.get("/items", request=Page, response=list[Item]).paginate(nanosdk.PageNumber())


# --- JSON lines, using the structured fields on each record ---------------------------

FIELDS = ("route", "method", "url", "attempt", "status_code", "elapsed_ms", "retry_delay", "page")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        line |= {key: record.__dict__[key] for key in FIELDS if key in record.__dict__}
        return json.dumps(line)


def main() -> None:
    def make_api(logger: logging.Logger | None = None) -> Api:
        return Api(
            "https://api.test",
            auth=Bearer("never-logged"),
            retry=nanosdk.Retry(backoff=0.05),
            logger=logger,
            transport=httpx.MockTransport(fake_api),
        )

    # 1. Plain text: retries at WARNING, everything at DEBUG.
    logging.basicConfig(
        level=logging.WARNING, stream=sys.stdout, format="%(levelname)-7s %(name)s: %(message)s"
    )
    logging.getLogger("nanosdk").setLevel(logging.DEBUG)
    print("--- plain text, nanosdk at DEBUG")
    list(make_api().items.iter())

    # 2. JSON lines, under an SDK's own logger name.
    global calls
    calls = 0
    sdk_logger = logging.getLogger("my_sdk")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    sdk_logger.addHandler(handler)
    sdk_logger.setLevel(logging.DEBUG)
    sdk_logger.propagate = False
    print("\n--- JSON, logger=my_sdk")
    list(make_api(sdk_logger).items.iter())


if __name__ == "__main__":
    main()

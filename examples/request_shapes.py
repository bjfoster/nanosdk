"""Where request-model fields end up on the wire, and what response types give back.

Prints the HTTP request each call produces (via ``build_request`` and an echo
server), so it doubles as a reference. No network.

Run:  python examples/request_shapes.py
"""

import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any

import httpx
from pydantic import BaseModel, ConfigDict, RootModel, ValidationError
from pydantic.alias_generators import to_camel

import nanosdk
from nanosdk import Body, File, Form, Header, Json, Path, Query


class Status(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


# --- request models -----------------------------------------------------------


class SearchIssues(BaseModel):
    """GET: unmarked fields -> query string; path placeholders -> path."""

    repo: str  # matches {repo} in the path
    labels: list[str] = []  # repeated: ?labels=a&labels=b
    state: Status = Status.OPEN  # enums -> their value
    since: date | None = None  # None -> omitted; dates -> ISO 8601
    include_drafts: bool = False  # bools -> true/false
    page_size: Annotated[int, Query(alias="per-page")] = 30  # rename on the wire
    request_id: Annotated[str | None, Header()] = None  # -> request-id: ...


class CreateIssue(BaseModel):
    """POST: unmarked fields -> keys of a JSON object body."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    repo: Annotated[str, Path()]
    title: str  # -> "title"
    assignee_ids: list[int] = []  # -> "assigneeIds" (alias_generator)
    due_at: datetime | None = None  # -> ISO 8601 string
    dry_run: Annotated[bool, Query()] = False  # in the query instead (still camelCased)


class EditIssue(BaseModel):
    """PATCH: only fields you actually pass are sent (exclude_unset)."""

    repo: str
    number: int
    title: str | None = None
    body: str | None = None
    milestone: int | None = None


class ReplaceLabels(BaseModel):
    """Body(): this one field *is* the whole body."""

    repo: str
    number: int
    labels: Annotated[list[str], Body()]


class Labels(RootModel[list[str]]):
    """A RootModel request: call the route with the value itself."""


class Comment(BaseModel):
    """Mixing JSON keys with headers: Json() is the explicit default for POST."""

    repo: str
    number: int
    text: Annotated[str, Json(alias="body")]
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")]


class UploadAttachment(BaseModel):
    """Form() + File() -> multipart/form-data."""

    repo: str
    caption: Annotated[str, Form()]
    file: Annotated[tuple[str, bytes, str], File()]  # (filename, content, content type)


class Login(BaseModel):
    """Form() alone -> application/x-www-form-urlencoded."""

    username: Annotated[str, Form()]
    password: Annotated[str, Form()]


class Issue(BaseModel):
    number: int
    title: str


# --- routes -------------------------------------------------------------------


class Tracker(nanosdk.Client):
    search = nanosdk.get("/repos/{repo}/issues", request=SearchIssues, response=list[Issue])
    create = nanosdk.post("/repos/{repo}/issues", request=CreateIssue, response=Issue)
    edit = nanosdk.patch("/repos/{repo}/issues/{number}", request=EditIssue, response=Issue)
    replace_labels = nanosdk.put(
        "/repos/{repo}/issues/{number}/labels", request=ReplaceLabels, response=list[str]
    )
    set_default_labels = nanosdk.put("/labels/default", request=Labels, response=Labels)
    comment = nanosdk.post("/repos/{repo}/issues/{number}/comments", request=Comment)
    attach = nanosdk.post("/repos/{repo}/attachments", request=UploadAttachment)
    login = nanosdk.post("/session", request=Login, response=dict[str, Any])

    # Response types other than models:
    readme = nanosdk.get("/readme", response=str)  # response.text
    avatar = nanosdk.get("/avatar.png", response=bytes)  # response.content
    raw = nanosdk.get("/anything", response=httpx.Response)  # the response itself
    ping = nanosdk.head("/", headers={"X-Probe": "1"})  # None; static route headers
    propfind = nanosdk.route("PROPFIND")("/dav", response=str)  # any method


def show(request: httpx.Request) -> None:
    print(f"{request.method} {request.url.raw_path.decode()}")
    for name in ("content-type", "request-id", "idempotency-key", "x-probe"):
        if name in request.headers:
            print(f"  {name}: {request.headers[name][:60]}")
    body = request.read()
    if request.headers.get("content-type", "").startswith("multipart/"):
        parts = re.findall(r'name="([^"]+)"(?:; filename="([^"]+)")?', body.decode("latin-1"))
        print("  parts:", ", ".join(f"{n} ({f})" if f else n for n, f in parts))
    elif body:
        print("  " + body.decode())
    print()


def echo(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/readme":
        return httpx.Response(200, text="# Tracker\n")
    if path == "/avatar.png":
        return httpx.Response(200, content=b"\x89PNG...")
    if path == "/labels/default":
        return httpx.Response(200, content=request.read())
    if path == "/dav":
        return httpx.Response(207, text="<multistatus/>")
    return httpx.Response(200, json={"echo": path})


def main() -> None:
    api = Tracker("https://tracker.test", transport=httpx.MockTransport(echo))

    show(
        api.search.build_request(
            repo="nanosdk",
            labels=["bug", "p1"],
            since=date(2026, 1, 1),
            page_size=50,
            request_id="req-42",
        )
    )
    due = datetime(2026, 11, 1, tzinfo=UTC)
    show(api.create.build_request(repo="nanosdk", title="Crash", assignee_ids=[7], due_at=due))
    show(api.create.build_request(repo="nanosdk", title="Try it", dry_run=True))
    show(api.edit.build_request(repo="nanosdk", number=12, milestone=None))  # sends only that
    show(api.replace_labels.build_request(repo="nanosdk", number=12, labels=["bug", "ui"]))
    show(api.set_default_labels.build_request(["triage"]))
    show(api.comment.build_request(repo="nanosdk", number=12, text="+1", idempotency_key="k1"))
    png = ("shot.png", b"\x89PNG\r\n", "image/png")
    show(api.attach.build_request(repo="nanosdk", caption="screenshot", file=png))
    show(api.login.build_request(username="ada", password="hunter2"))
    show(api.ping.build_request())

    print("responses:")
    print("  str:       ", repr(api.readme()))
    print("  bytes:     ", api.avatar())
    print("  Response:  ", api.raw().json())
    print("  RootModel: ", api.set_default_labels(["triage"]))
    print("  None:      ", api.ping())
    print("  PROPFIND:  ", api.propfind())

    # Arguments are validated by pydantic *before* anything is sent:
    try:
        api.search(repo="nanosdk", state="reopened")  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    except ValidationError as exc:
        print(f"\nrejected before sending: {exc.errors()[0]['msg']}")


if __name__ == "__main__":
    main()

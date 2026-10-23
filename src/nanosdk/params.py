"""Field markers that say *where* a request-model field goes on the wire.

Use them with `typing.Annotated` on fields of a pydantic request model:

```python
class GetRepo(BaseModel):
    owner: Annotated[str, Path()]
    repo: Annotated[str, Path()]
    per_page: Annotated[int, Query(alias="per-page")] = 30
    trace_id: Annotated[str | None, Header()] = None
```

Unmarked fields are placed automatically:

- a field whose name matches a `{placeholder}` in the route path goes in the path
- on GET / DELETE / HEAD / OPTIONS, in the query string
- on POST / PUT / PATCH, as a key in the JSON object body

Wire names follow the model's pydantic aliases (`Field(alias=...)`,
`serialization_alias`, `alias_generator`); a marker's own `alias` overrides them.
"""

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Body", "File", "Form", "Header", "Json", "Location", "Param", "Path", "Query"]


class Location(StrEnum):
    """Where a request-model field is sent."""

    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    JSON = "json"  # one key inside a JSON object body
    BODY = "body"  # the whole body
    FORM = "form"
    FILE = "file"


@dataclass(frozen=True, slots=True)
class Param:
    """Base class of all field markers.

    Args:
        alias: Name to use on the wire instead of the field's (alias) name.
        location: Where the field is sent; fixed by each subclass.
    """

    alias: str | None = None
    location: Location = Location.QUERY  # overridden by subclasses


@dataclass(frozen=True, slots=True)
class Path(Param):
    """Substituted into the matching `{name}` placeholder of the route path, URL-encoded.

    Only needed when the field name doesn't match the placeholder:
    `user_id: Annotated[int, Path(alias="userId")]` fills `/users/{userId}`.
    """

    location: Location = Location.PATH


@dataclass(frozen=True, slots=True)
class Query(Param):
    """Sent as a query-string parameter.

    `None` values are omitted, lists and tuples repeat the key (`?tag=a&tag=b`) and
    booleans become `true` / `false`. Use it to put a field of a POST/PUT/PATCH route in
    the query instead of the body.
    """

    location: Location = Location.QUERY


@dataclass(frozen=True, slots=True)
class Header(Param):
    """Sent as a request header; `None` values are omitted.

    Without an `alias`, underscores become dashes: `x_trace_id` is sent as `x-trace-id`.
    """

    location: Location = Location.HEADER


@dataclass(frozen=True, slots=True)
class Json(Param):
    """A key inside the JSON object body.

    This is already the default for unmarked fields on POST / PUT / PATCH; use it to
    rename a key (`Json(alias="body")`) or to send JSON on another method.
    """

    location: Location = Location.JSON


@dataclass(frozen=True, slots=True)
class Body(Param):
    """This field *is* the entire request body.

    Models, lists and dicts are sent as JSON; `str` and `bytes` are sent as-is. At most
    one field per request model can be `Body()`, and it can't be combined with `Json()`
    fields.
    """

    location: Location = Location.BODY


@dataclass(frozen=True, slots=True)
class Form(Param):
    """Sent as a form field.

    The body is `application/x-www-form-urlencoded`, or `multipart/form-data` when the
    model also has [`File`][nanosdk.File] fields.
    """

    location: Location = Location.FORM


@dataclass(frozen=True, slots=True)
class File(Param):
    """Sent as a file in a `multipart/form-data` body.

    The field's value is anything httpx accepts as a file: `bytes`, a binary file object,
    or a tuple `(filename, content)` / `(filename, content, content_type)`.
    """

    location: Location = Location.FILE

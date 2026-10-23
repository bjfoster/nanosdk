"""Field markers that say *where* a request-model field goes on the wire.

Use them with ``typing.Annotated`` on fields of a pydantic request model::

    class GetRepo(BaseModel):
        owner: Annotated[str, Path()]
        repo: Annotated[str, Path()]
        per_page: Annotated[int, Query(alias="per-page")] = 30
        trace_id: Annotated[str | None, Header()] = None

Unmarked fields are placed automatically:

* a field whose name matches a ``{placeholder}`` in the route path -> path
* on GET / DELETE / HEAD / OPTIONS -> query string
* on POST / PUT / PATCH -> a key in the JSON object body
"""

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Body", "File", "Form", "Header", "Json", "Location", "Param", "Path", "Query"]


class Location(StrEnum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    JSON = "json"  # one key inside a JSON object body
    BODY = "body"  # the whole body
    FORM = "form"
    FILE = "file"


@dataclass(frozen=True, slots=True)
class Param:
    """Base class of all field markers."""

    alias: str | None = None
    location: Location = Location.QUERY  # overridden by subclasses


@dataclass(frozen=True, slots=True)
class Path(Param):
    """Substituted into ``{name}`` in the route path (URL-encoded)."""

    location: Location = Location.PATH


@dataclass(frozen=True, slots=True)
class Query(Param):
    """Sent as a query-string parameter. ``None`` values are omitted; lists repeat."""

    location: Location = Location.QUERY


@dataclass(frozen=True, slots=True)
class Header(Param):
    """Sent as a request header. Without an alias, ``x_trace_id`` becomes ``x-trace-id``."""

    location: Location = Location.HEADER


@dataclass(frozen=True, slots=True)
class Json(Param):
    """A key inside the JSON object body (the default for body methods)."""

    location: Location = Location.JSON


@dataclass(frozen=True, slots=True)
class Body(Param):
    """This field *is* the entire request body (a model, list, ``str`` or ``bytes``)."""

    location: Location = Location.BODY


@dataclass(frozen=True, slots=True)
class Form(Param):
    """Sent as an ``application/x-www-form-urlencoded`` (or multipart) form field."""

    location: Location = Location.FORM


@dataclass(frozen=True, slots=True)
class File(Param):
    """Sent as a multipart file. Value is ``bytes`` or ``(filename, bytes[, content_type])``."""

    location: Location = Location.FILE

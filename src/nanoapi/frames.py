"""Turn decoded responses into DataFrames.

Needs the matching extra: ``nanoapi[polars]`` or ``nanoapi[pandas]``. Use as a route
``.map()`` or on anything iterable of models / dicts (including ``route.iter(...)``)::

    posts = nanoapi.get("/posts", response=list[Post]).map(nanoapi.frames.polars)
    df = nanoapi.frames.pandas(api.list_posts.iter())
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, RootModel

if TYPE_CHECKING:
    import pandas as pd
    import polars as pl

__all__ = ["pandas", "polars", "records"]

type Rows = BaseModel | Mapping[str, Any] | Iterable[BaseModel | Mapping[str, Any]]


def records(data: Rows) -> list[dict[str, Any]]:
    """Plain-dict rows: models are dumped with Python field names, nested models as dicts."""
    if isinstance(data, BaseModel):
        dumped: Any = data.model_dump()
        return records(dumped) if isinstance(data, RootModel) else [dumped]
    if isinstance(data, Mapping):
        return [_row(cast(object, data))]
    return [_row(row) for row in data]


def polars(data: Rows) -> pl.DataFrame:
    import polars as pl

    rows = records(data)
    return pl.from_dicts(rows) if rows else pl.DataFrame()


def pandas(data: Rows) -> pd.DataFrame:
    import pandas as pd

    return pd.DataFrame.from_records(records(data))


def _row(row: object) -> dict[str, Any]:
    if isinstance(row, BaseModel):
        return row.model_dump()
    if isinstance(row, Mapping):
        return dict(cast(Mapping[str, Any], row))
    raise TypeError(f"expected a pydantic model or a mapping per row, got {type(row).__name__}")

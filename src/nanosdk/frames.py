"""Turn decoded responses into DataFrames.

Use the converters as a route [`map`][nanosdk.Route.map], so the route returns a
DataFrame, or call them on anything iterable of models or dicts, including a
paginated route's `.iter()`:

```python
class Api(nanosdk.Client):
    posts = nanosdk.get("/posts", response=list[Post]).map(nanosdk.frames.polars)

df = api.posts()                                    # pl.DataFrame
df = nanosdk.frames.pandas(api.list_posts.iter())   # every page, one pd.DataFrame
```

polars and pandas are optional: install `nanosdk[polars]` or `nanosdk[pandas]`.
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
"""What the converters accept: a model, a mapping, or an iterable of either.

A `RootModel` (say `RootModel[list[Post]]`) is unwrapped to its root value.
"""


def records(data: Rows) -> list[dict[str, Any]]:
    """Convert models and mappings to plain-dict rows.

    Models are dumped with their Python field names (not wire aliases), and nested
    models become nested dicts. Useful for CSV, SQLite or any other row consumer.

    Args:
        data: A model, a mapping, or an iterable of them.

    Returns:
        One dict per row; a single model or mapping gives one row.

    Raises:
        TypeError: If an item is neither a pydantic model nor a mapping.
    """
    if isinstance(data, BaseModel):
        dumped: Any = data.model_dump()
        return records(dumped) if isinstance(data, RootModel) else [dumped]
    if isinstance(data, Mapping):
        return [_row(cast(object, data))]
    return [_row(row) for row in data]


def polars(data: Rows) -> pl.DataFrame:
    """Build a polars DataFrame, one row per model or mapping.

    Nested models become struct columns. Needs the `nanosdk[polars]` extra.

    Args:
        data: A model, a mapping, or an iterable of them.

    Returns:
        The DataFrame; empty (no columns) when there are no rows.
    """
    import polars as pl

    rows = records(data)
    return pl.from_dicts(rows) if rows else pl.DataFrame()


def pandas(data: Rows) -> pd.DataFrame:
    """Build a pandas DataFrame, one row per model or mapping.

    Nested models become dict-valued columns. Needs the `nanosdk[pandas]` extra.

    Args:
        data: A model, a mapping, or an iterable of them.

    Returns:
        The DataFrame.
    """
    import pandas as pd

    return pd.DataFrame.from_records(records(data))


def _row(row: object) -> dict[str, Any]:
    if isinstance(row, BaseModel):
        return row.model_dump()
    if isinstance(row, Mapping):
        return dict(cast(Mapping[str, Any], row))
    raise TypeError(f"expected a pydantic model or a mapping per row, got {type(row).__name__}")

"""Responses straight into DataFrames with ``.map()`` and ``nanosdk.frames``.

Run:  pip install "nanosdk[polars,pandas]"
      python examples/dataframes.py
"""

from collections import Counter
from typing import Annotated

import pandas as pd
import polars as pl
from pydantic import BaseModel, Field

import nanosdk
from nanosdk import frames

# --- models (wire names are camelCase; DataFrame columns use the Python names) --


class Todo(BaseModel):
    user_id: int = Field(alias="userId")
    id: int
    title: str
    completed: bool


class Comment(BaseModel):
    post_id: int = Field(alias="postId")
    id: int
    name: str
    email: str
    body: str


class PageParams(BaseModel):
    page: Annotated[int, nanosdk.Query(alias="_page")] = 1
    limit: Annotated[int, nanosdk.Query(alias="_limit")] = 100


# --- any function works as a map; its return type becomes the route's ----------


def completion_by_user(todos: list[Todo]) -> dict[int, float]:
    done, total = Counter[int](), Counter[int]()
    for todo in todos:
        total[todo.user_id] += 1
        done[todo.user_id] += todo.completed
    return {user: done[user] / total[user] for user in sorted(total)}


# --- client -----------------------------------------------------------------


class Placeholder(nanosdk.Client):
    todos = nanosdk.get("/todos", response=list[Todo])  # -> list[Todo]
    todos_pl = todos.map(frames.polars)  # -> pl.DataFrame
    todos_pd = todos.map(frames.pandas)  # -> pd.DataFrame
    completion = todos.map(completion_by_user)  # -> dict[int, float]
    comments = nanosdk.get("/comments", request=PageParams, response=list[Comment]).paginate(
        nanosdk.PageNumber("page", size="limit")
    )

    def __init__(self) -> None:
        super().__init__("https://jsonplaceholder.typicode.com")


def main() -> None:
    with Placeholder() as api:
        # polars
        todos: pl.DataFrame = api.todos_pl()
        print(todos.head(3))
        print(
            todos.group_by("user_id")
            .agg(pl.col("completed").mean().round(2).alias("done"))
            .sort("done", descending=True)
            .head(3)
        )

        # pandas
        todos_pd: pd.DataFrame = api.todos_pd()
        print(f"\npandas: {len(todos_pd)} rows, columns {list(todos_pd.columns)}")
        print(f"overall completion: {todos_pd['completed'].mean():.0%}")

        # plain Python mapping
        best_user, rate = max(api.completion().items(), key=lambda kv: kv[1])
        print(f"\nmost productive: user {best_user} ({rate:.0%} done)")

        # every page of a paginated route -> one frame (5 pages of 100, then an empty one)
        comments = frames.polars(api.comments.iter(limit=100))
        domains = (
            comments.with_columns(pl.col("email").str.split(".").list.last().alias("tld"))
            .group_by("tld")
            .len()
            .sort("len", descending=True)
        )
        print(f"\n{comments.height} comments; email TLDs:\n{domains.head(4)}")

        # or just the rows, for anything else (csv, sqlite, ...)
        print("\nfirst record:", frames.records(api.todos()[:1]))


if __name__ == "__main__":
    main()

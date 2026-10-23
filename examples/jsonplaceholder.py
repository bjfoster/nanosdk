"""A complete SDK for https://jsonplaceholder.typicode.com in ~40 lines.

Run:  python examples/jsonplaceholder.py
"""

import asyncio
from typing import Annotated

from pydantic import BaseModel, Field

import nanoapi

# --- models -----------------------------------------------------------------


class Post(BaseModel):
    id: int
    user_id: int = Field(alias="userId")
    title: str
    body: str


class PostId(BaseModel):
    post_id: int


class ListPosts(BaseModel):
    user_id: Annotated[int | None, nanoapi.Query(alias="userId")] = None


class NewPost(BaseModel):
    title: str
    body: str
    user_id: int = Field(serialization_alias="userId")


class EditPost(BaseModel):
    post_id: int
    title: str | None = None
    body: str | None = None


# --- routes (shared by the sync and async clients) --------------------------


class Posts(nanoapi.Routes, prefix="/posts"):
    list_posts = nanoapi.get("", request=ListPosts, response=list[Post])
    get_post = nanoapi.get("/{post_id}", request=PostId, response=Post)
    create_post = nanoapi.post("", request=NewPost, response=Post)
    edit_post = nanoapi.patch("/{post_id}", request=EditPost, response=Post)
    delete_post = nanoapi.delete("/{post_id}", request=PostId)


class JSONPlaceholder(Posts, nanoapi.Client):
    def __init__(self) -> None:
        super().__init__("https://jsonplaceholder.typicode.com")


class AsyncJSONPlaceholder(Posts, nanoapi.AsyncClient):
    def __init__(self) -> None:
        super().__init__("https://jsonplaceholder.typicode.com")


def main() -> None:
    with JSONPlaceholder() as api:
        post = api.get_post(post_id=1)  # Post
        print(post.title)
        mine = api.list_posts(user_id=1)  # list[Post]
        print(len(mine), "posts by user 1")
        new = api.create_post(title="hi", body="there", user_id=1)
        print("created", new.id)
        api.edit_post(post_id=1, title="renamed")  # PATCH {"title": "renamed"}
        api.delete_post(post_id=1)  # None


async def amain() -> None:
    async with AsyncJSONPlaceholder() as api:
        posts = await asyncio.gather(*(api.get_post(post_id=i) for i in range(1, 4)))
        print([p.id for p in posts])


if __name__ == "__main__":
    main()
    asyncio.run(amain())

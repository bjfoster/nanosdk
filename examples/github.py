"""GitHub REST API: Link-header pagination, optional token auth, typed error bodies.

Run:  python examples/github.py
      GITHUB_TOKEN=... python examples/github.py   # higher rate limits
"""

import os
from datetime import datetime
from itertools import islice
from typing import Literal

from pydantic import BaseModel

import nanosdk
from nanosdk.auth import Bearer

# --- models -----------------------------------------------------------------


class User(BaseModel):
    login: str
    id: int
    name: str | None = None
    public_repos: int = 0


class Repo(BaseModel):
    full_name: str
    stargazers_count: int
    language: str | None
    updated_at: datetime
    fork: bool


class SearchResult(BaseModel):
    total_count: int
    items: list[Repo]


class GitHubError(BaseModel):
    message: str
    documentation_url: str | None = None


class GetUser(BaseModel):
    username: str


class ListRepos(BaseModel):
    username: str
    type: Literal["all", "owner", "member"] = "owner"
    sort: Literal["created", "updated", "pushed", "full_name"] = "updated"
    per_page: int = 30


class SearchRepos(BaseModel):
    q: str
    sort: Literal["stars", "forks", "updated"] | None = None
    per_page: int = 30


# --- client -----------------------------------------------------------------


class GitHub(nanosdk.Client):
    get_user = nanosdk.get("/users/{username}", request=GetUser, response=User)

    # GitHub paginates with `Link: <...?page=2>; rel="next"` headers.
    list_repos = nanosdk.get(
        "/users/{username}/repos", request=ListRepos, response=list[Repo]
    ).paginate(nanosdk.LinkHeader())

    # Search wraps the list in an envelope, so say where the items live.
    search_repos = nanosdk.get(
        "/search/repositories", request=SearchRepos, response=SearchResult
    ).paginate(nanosdk.LinkHeader(), items=lambda result: result.items)

    def __init__(self, token: str | None = None) -> None:
        super().__init__(
            "https://api.github.com",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            auth=Bearer(token) if token else None,
            error_model=GitHubError,  # 4xx/5xx bodies -> APIStatusError.error
            retry=nanosdk.Retry(),  # 429s and 5xx, honouring Retry-After
        )


def main() -> None:
    with GitHub(os.environ.get("GITHUB_TOKEN")) as gh:
        user = gh.get_user(username="octocat")
        print(f"{user.login} ({user.name}): {user.public_repos} public repos\n")

        # per_page=5 -> nanosdk follows the Link header across two pages.
        print("octocat's repos, most recently updated first:")
        for repo in gh.list_repos.iter(username="octocat", per_page=5):
            print(f"  {repo.full_name:<40} ★{repo.stargazers_count:<6} {repo.language or '-'}")

        # Paginated iterators are lazy: islice stops after 8 items = 2 requests.
        print("\nmost-starred Python repos:")
        query = "language:python stars:>50000"
        for repo in islice(gh.search_repos.iter(q=query, sort="stars", per_page=5), 8):
            print(f"  ★{repo.stargazers_count:>7,}  {repo.full_name}")

        # The first page on its own, envelope and all:
        first = gh.search_repos(q=query, per_page=1)
        print(f"\n{first.total_count} repos match {query!r}")

        try:
            gh.get_user(username="this-user-should-not-exist-4f3a9")
        except nanosdk.APIStatusError as exc:
            error: GitHubError | None = exc.error
            print(f"\n{exc.status_code}: {error.message if error else exc.body}")


if __name__ == "__main__":
    main()

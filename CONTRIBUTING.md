# Contributing

## Setup

```bash
git clone git@github.com:bjfoster/nanosdk.git && cd nanosdk
uv sync                              # Python 3.13 venv with the dev tools
uv run pre-commit install            # hooks: ruff, file hygiene, lockfile, commit messages
```

## Checks

The pre-commit hooks run ruff and friends on every commit, and the type checkers before
each push. CI runs everything below on Linux, macOS and Windows.

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest                        # unit tests (offline, httpx.MockTransport)
uv run pyright && uv run mypy && uv run ty check   # all strict; tests/typecheck.py
                                     # holds assert_type and must-fail cases
uv run --group docs mkdocs build --strict          # API docs from the docstrings
uv run pytest -m e2e                 # opt-in: generate clients for 5 real-world specs,
                                     # strict-pyright them, call the live APIs
```

Every public object in `src/` needs a Google-style docstring (ruff's `D` rules): the API
reference is generated from them. `uv run --group docs mkdocs serve` previews the docs.

The examples in `examples/` are checked too. `examples/petstore-workspace` and
`examples/realtime-trains` are separate uv projects with their own lockfiles:

```bash
cd examples/realtime-trains && uv run pytest && uv run pyright
```

## Commit messages and releases

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (a commit-msg
hook checks them): `feat: ...`, `fix: ...`, `docs: ...`, `refactor!: ...` for breaking
changes. Pull requests are squash-merged with the PR title as the commit message, so the
PR title must be conventional too (CI checks it).

Releases are automated with [release-please](https://github.com/googleapis/release-please):
every push to `main` updates a release PR with the next version (from the commit types)
and the changelog. Merging that PR tags the release, publishes it to PyPI (trusted
publishing, no tokens) and deploys the docs.

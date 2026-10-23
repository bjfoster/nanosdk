# Petstore workspace: an OpenAPI SDK generated with `nanoapi generate`, in a uv workspace

A [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) with two packages
sharing one lockfile and one virtualenv:

```
petstore-workspace/
├── pyproject.toml              # workspace root: members, dev tools, ruff/pyright/pytest config
├── uv.lock
└── packages/
    ├── petstore/               # the SDK (a library)
    │   ├── openapi.json        # vendored spec
    │   ├── pyproject.toml      # [tool.nanoapi.codegen] lives here
    │   ├── src/petstore/
    │   │   ├── _generated/     # GENERATED from openapi.json by `nanoapi generate`
    │   │   │   ├── models.py   #   pydantic models (via datamodel-codegen)
    │   │   │   └── client.py   #   parameter models, routes, PetstoreBase clients
    │   │   ├── client.py       # hand-written: auth, retries, error model, one fix
    │   │   └── __init__.py
    │   └── tests/test_petstore.py
    └── petstore-demo/          # an app that depends on the SDK
        └── src/petstore_demo/__init__.py
```

Everything about the API's shape comes from the spec: models, every route and its
parameters, and sync/async base clients. The hand-written `client.py` is about 80 lines
of what a spec can't say (auth, retries, the error model, and one place the live server
is stricter than the spec). pyright, mypy and ty check the result end to end.

## Try it

```bash
cd examples/petstore-workspace
uv sync                      # one .venv for both packages + dev tools
uv run petstore-demo         # talks to the live https://petstore3.swagger.io
uv run pytest                # offline tests + "is _generated/ up to date?" check
uv run pyright && uv run ty check
```

## How it was set up

The workspace root is *virtual*: its `pyproject.toml` has no `[project]`, just the member
list. Everything else came from `uv` commands:

```bash
mkdir petstore-workspace && cd petstore-workspace
printf '[tool.uv.workspace]\nmembers = ["packages/*"]\n' > pyproject.toml
echo 3.13 > .python-version

# Members: `uv init` inside a workspace registers them automatically.
uv init --lib     --python 3.13 --no-pin-python --name petstore      packages/petstore
uv init --package --python 3.13 --no-pin-python --name petstore-demo packages/petstore-demo

# SDK dependencies. nanoapi comes from this repo as an editable path dependency;
# in a real project that's just `uv add --package petstore nanoapi`.
uv add --package petstore pydantic
uv add --package petstore --editable ../..

# The app depends on the SDK *as a workspace member* ({ workspace = true } source).
uv add --package petstore-demo petstore

# Dev tools live at the root and are shared by every member.
uv add --dev "nanoapi[codegen]" pytest pytest-asyncio pyright ty mypy ruff
```

Pass `--python 3.13` to `uv init`. Without it, `requires-python` follows the newest
interpreter uv finds, which may be newer than you meant to require.

## Generating the client

```bash
uv run --directory packages/petstore nanoapi generate          # regenerate _generated/
uv run --directory packages/petstore nanoapi generate --check  # CI: is it up to date?
```

The settings are in `packages/petstore/pyproject.toml`:

```toml
[tool.nanoapi.codegen]
spec = "openapi.json"
output = "src/petstore/_generated"
name = "PetstoreBase"
base-url = "https://petstore3.swagger.io/api/v3"  # the spec only has a relative server URL
skip = ["updatePetWithForm"]  # hand-written in client.py: the server is stricter than the spec

[tool.nanoapi.codegen.rename]  # keep the SDK's established method names
getPetById = "get_pet"
findPetsByStatus = "find_by_status"
...
```

From each operation the generator writes a parameter model with the spec's wire names,
`Literal` enums and constraints (`pet_id` is sent as the `petId` path segment), and a
route grouped by tag. When an operation's only input is a JSON model, the model itself is
the request, so `add_pet(name="rex", photo_urls=[...])` works directly. Inline schemas get
stable names (`GetPetByIdResponse`) before datamodel-codegen runs.

`test_generated_code_is_up_to_date` runs `nanoapi generate --check`, so CI fails if
someone edits `_generated/` by hand or changes `openapi.json` without regenerating.

## The hand-written part

`client.py` subclasses the generated `PetstoreBase` / `AsyncPetstoreBase`:

```python
class HandWritten(nanoapi.Routes):
    # Skipped by the generator: the spec says `name` is optional, the server 400s without it.
    update_pet_form = nanoapi.post("/pet/{pet_id}", request=UpdatePetForm, response=Pet)

class Petstore(HandWritten, PetstoreBase):
    def __init__(self, base_url=DEFAULT_BASE_URL, *, api_key="special-key", retry=..., transport=None):
        super().__init__(base_url, auth=ApiKey("api_key", api_key), error_model=ApiResponse, ...)
```

Things learned running this against the real server:

- **The spec and the server disagree sometimes.** Rather than edit generated code, `skip`
  the operation in the config and declare it by hand. The generator still writes its
  request and response models.
- **Shared demo data can break the spec.** Other users create pets without `photoUrls`,
  which the spec requires, so `find_by_status` may raise `ResponseValidationError` naming
  the bad records. The models enforce the spec; the demo catches that.
- **`error_model=ApiResponse`** reuses a generated model for error bodies, so
  `APIStatusError.error` is typed. The demo uses it when the shared server's
  `/store/inventory` returns 500.
- The spec only has a relative server URL (`/api/v3`), so `base-url` supplies the real one.

## Using it from another project

The SDK is an ordinary package (`uv build --package petstore` builds a wheel). Other
workspace members depend on it with `{ workspace = true }`, as `petstore-demo` does. Outside
the workspace, install it from a git URL or a package index.

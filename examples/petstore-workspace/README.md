# Petstore workspace: an OpenAPI SDK with uv + datamodel-codegen + nanoapi

A [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) with two packages
sharing one lockfile and one virtualenv:

```
petstore-workspace/
├── pyproject.toml              # workspace root: members, dev tools, ruff/pyright/pytest config
├── uv.lock
└── packages/
    ├── petstore/               # the SDK (a library)
    │   ├── openapi.json        # vendored spec
    │   ├── pyproject.toml      # [tool.datamodel-codegen] lives here
    │   ├── src/petstore/
    │   │   ├── models.py       # GENERATED from openapi.json
    │   │   ├── client.py       # hand-written: parameter models + nanoapi routes
    │   │   └── __init__.py
    │   └── tests/test_petstore.py
    └── petstore-demo/          # an app that depends on the SDK
        └── src/petstore_demo/__init__.py
```

The models come from the spec; the routes are about 60 lines you write once. The result is
a sync and async client that pyright, mypy and ty all check end to end.

## Try it

```bash
cd examples/petstore-workspace
uv sync                      # one .venv for both packages + dev tools
uv run petstore-demo         # talks to the live https://petstore3.swagger.io
uv run pytest                # offline tests + "is models.py up to date?" check
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
uv add --dev "datamodel-code-generator[ruff]" pytest pytest-asyncio pyright ty mypy ruff
```

Pass `--python 3.13` to `uv init`. Without it, `requires-python` follows the newest
interpreter uv finds, which may be newer than you meant to require.

## Generating the models

`packages/petstore/pyproject.toml` holds the codegen settings, so regenerating is one
command with no flags to remember:

```bash
curl -o packages/petstore/openapi.json https://petstore3.swagger.io/api/v3/openapi.json
uv run --directory packages/petstore datamodel-codegen
```

The settings that matter most:

| setting | why |
|---|---|
| `output-model-type = "pydantic_v2.BaseModel"`, `target-python-version = "3.13"` | modern pydantic + `X \| None`, `list[...]`, `StrEnum` |
| `snake-case-field` + `alias-generator = "to_camel"` + `allow-population-by-field-name` | `pet.photo_urls` in Python, `photoUrls` on the wire. Using the alias generator instead of per-field `Field(alias=...)` matters: type checkers then see `Pet(photo_urls=...)` as the constructor signature, and nanoapi copies that signature onto the route |
| `naming-strategy = "parent-prefixed"` | the spec has two inline `status` enums; you get `PetStatus` / `OrderStatus`, not `Status` / `Status1` |
| `use-annotated`, `field-constraints` | `Annotated[int, Field(...)]` instead of `conint()` |
| `formatters = ["ruff-format", "ruff-check"]` | output is already formatted; the root ruff config excludes `models.py` so nothing else restyles it |
| `custom-file-header`, `disable-timestamp` | a "don't edit" note and stable output, so regenerating gives no diff unless the spec changed |

`test_generated_models_are_up_to_date` regenerates the models and compares them with
`models.py`. CI fails if someone edits the generated file by hand, or updates
`openapi.json` without regenerating. It writes the fresh copy *next to* `models.py`: codegen's
ruff formatter uses whatever ruff config applies at the output path, so a temp directory
elsewhere would wrap lines differently.

## The hand-written part

datamodel-codegen generates the schemas (`components/schemas`): Pet, Order, User, and so on.
Each operation's path, query and header parameters are small request models in `client.py`:

```python
class PetRoutes(nanoapi.Routes, prefix="/pet"):
    # A generated model used directly as the request: its fields become the JSON body.
    add_pet = nanoapi.post("", request=Pet, response=Pet, exclude_unset=True)
    get_pet = nanoapi.get("/{pet_id}", request=PetId, response=Pet)
    find_by_status = nanoapi.get("/findByStatus", request=FindByStatus, response=list[Pet])
    ...

class Petstore(PetRoutes, StoreRoutes, UserRoutes, nanoapi.Client): ...
class AsyncPetstore(PetRoutes, StoreRoutes, UserRoutes, nanoapi.AsyncClient): ...
```

```python
api.add_pet(name="Biscuit", photo_urls=["..."], status=PetStatus.AVAILABLE)  # -> Pet
```

Lessons from running this against the real server:

- **`exclude_unset=True` on create/update routes.** Generated models make most fields
  `X | None = None`. Without it, every optional field you didn't pass is sent as `null`.
- **Check each parameter's `in:`.** `POST /pet/{petId}` takes `name` and `status` in the
  query. On a POST, unmarked fields go into the JSON body, so they need `nanoapi.Query()`.
- **The spec and the server disagree sometimes.** The spec marks `name` optional there, but
  the server returns 400 without it. The hand-written parameter model is where you encode
  what the server actually does, with a comment.
- **`error_model=ApiResponse`** reuses a generated model for error bodies, so
  `APIStatusError.error` is typed. The demo uses it when the shared server's
  `/store/inventory` returns 500.
- Path placeholders can use snake_case (`/{pet_id}`) even when the spec says `{petId}`: only
  the resulting URL has to match.

## Using it from another project

The SDK is an ordinary package (`uv build --package petstore` builds a wheel). Other
workspace members depend on it with `{ workspace = true }`, as `petstore-demo` does. Outside
the workspace, install it from a git URL or a package index.

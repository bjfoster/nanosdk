# Examples

Each file is a standalone script, fully typed and checked by pyright, mypy and ty in strict
mode along with the library itself.

| Example | Network | Shows |
|---|---|---|
| [`jsonplaceholder.py`](jsonplaceholder.py) | live | the basics: a small CRUD SDK, sync + async, page-number pagination, retries |
| [`github.py`](github.py) | live | `LinkHeader` pagination, `items=` for search envelopes, `Bearer` from `GITHUB_TOKEN`, typed `error_model`, lazy `islice` |
| [`pokeapi.py`](pokeapi.py) | live | a hand-written strategy that follows a next-page URL, async fan-out with `asyncio.gather`, polars analysis |
| [`dataframes.py`](dataframes.py) | live | `.map(frames.polars)` / `.map(frames.pandas)`, plain-function maps, every page into one frame, `frames.records` |
| [`pagination.py`](pagination.py) | offline | all strategies side by side (`Offset`, `PageNumber`, `Cursor`, `LinkHeader`, Stripe-style custom), `.pages()`, laziness, ad hoc and async iteration |
| [`custom_client.py`](custom_client.py) | offline | HMAC request signing in `prepare_request`, envelope unwrapping in `process_response`, domain exceptions from `raise_for_status`, an idempotency-aware `Retry` subclass, per-route `meta` |
| [`auth_flows.py`](auth_flows.py) | offline | basic, `Bearer` (static and rotating), `ApiKey` (header and query), a custom `httpx.Auth`, `with_options`, OAuth 2 client credentials with caching and refresh-on-401, async |
| [`request_shapes.py`](request_shapes.py) | offline | where each field lands on the wire: path, query, headers, JSON, `Body()`, `RootModel`, forms, multipart files, aliases, PATCH `exclude_unset`, non-model responses |
| [`petstore-workspace/`](petstore-workspace/README.md) | live + offline | a uv workspace with an SDK package generated from an OpenAPI spec by datamodel-codegen, a demo app that depends on it, and a test that fails if the generated models drift from the spec |
| [`test_my_sdk.py`](test_my_sdk.py) | offline | testing your own SDK with pytest + `httpx.MockTransport`: requests, pagination, errors, timeouts, retries |

```bash
uv run python examples/pagination.py
uv run pytest examples/test_my_sdk.py
```

The live examples call public APIs that need no key: jsonplaceholder.typicode.com,
api.github.com (60 unauthenticated requests per hour) and pokeapi.co. `pokeapi.py` and
`dataframes.py` use the `polars` / `pandas` extras. The offline ones run against a small fake
server passed in as `transport=httpx.MockTransport(...)`; point `base_url` at a real API and
drop the transport to use them for real.

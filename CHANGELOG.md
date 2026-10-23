# Changelog

## 0.1.0 (2026-10-23)

The first release of nanosdk: typed API clients on httpx and pydantic. Declare routes
yourself, or generate them from an OpenAPI spec; either way pyright, mypy and ty check
every call end to end.

### Features

* **Typed routes.** `nanosdk.get/post/put/patch/delete(...)` tie a path to a pydantic request model and a response type. Calling a route is typed exactly like constructing its request model, and returns the declared response type.
* **Sync and async clients** (`Client`, `AsyncClient`) share route declarations through `Routes` groups (with path prefixes). A client instance exposes only your routes plus `with_options()` and `close()`; `route.bind(client)` calls routes that aren't declared on the class.
* **Request fields where the API wants them:** path, query, header, JSON key, whole body, form and file markers, pydantic aliases, and PATCH bodies that send only the fields you set.
* **Responses:** models, lists, dicts, `str`, `bytes` or the raw `httpx.Response`; `.map()` post-processes them with the types following along; `nanosdk.frames` turns them into polars or pandas DataFrames (`nanosdk[polars]`, `nanosdk[pandas]`).
* **Pagination:** `.iter()` and `.pages()` across pages with `Cursor`, `Offset`, `PageNumber`, `LinkHeader` (RFC 8288) or your own strategy, sync and async.
* **Retries:** `Retry` with exponential backoff, jitter and `Retry-After`, for idempotent methods by default.
* **Auth:** `Bearer`, `ApiKey` and `OAuth2ClientCredentials` (token caching and refresh on 401), for sync and async clients alike.
* **Hooks** for signing, envelopes and error mapping (`_prepare_request`, `_process_response`, `_raise_for_status`, `_retry_policy`), plus per-route `meta`.
* **Errors:** one `NanoSDKError` hierarchy, with 4xx/5xx bodies parsed into your `error_model`.
* **Logging** through the standard `logging` module with structured fields; tokens and API keys never reach nanosdk's logs.
* **`nanosdk generate`** (`nanosdk[codegen]`) turns an OpenAPI 3.x spec into a typed client package: models (via datamodel-codegen), a parameter model per operation, routes and clients. `rename`, `skip`, `transform` and `lenient` handle real-world specs, and `--check` catches drift in CI. Tested end to end on GitHub, PokéAPI, the US National Weather Service, Open-Meteo and APIs.guru.

### Documentation

* A guide and an API reference generated from the docstrings.
* Runnable examples: SDKs for GitHub, PokéAPI and JSONPlaceholder; offline walkthroughs of pagination, auth, hooks, request shapes, logging and testing; and two complete generated SDKs (Swagger Petstore in a uv workspace, and Realtime Trains, whose spec needs fixing first).

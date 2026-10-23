# API reference

Generated from the docstrings in the source. Everything is importable from the top-level
`nanoapi` package, except the [`nanoapi.auth`](auth.md) and [`nanoapi.frames`](frames.md)
modules.

| Page | Contents |
|---|---|
| [Clients](client.md) | `Client`, `AsyncClient` and the bound routes they hand out |
| [Routes](routes.md) | `get` / `post` / ..., `route`, `Verb`, `Route`, `PaginatedRoute`, `Routes` |
| [Request fields](params.md) | `Path`, `Query`, `Header`, `Json`, `Body`, `Form`, `File` markers |
| [Pagination](pagination.md) | `Page` and the `Cursor`, `Offset`, `PageNumber`, `LinkHeader` strategies |
| [Retries](retry.md) | `Retry` |
| [Auth](auth.md) | `Bearer`, `ApiKey`, `OAuth2ClientCredentials` |
| [DataFrames](frames.md) | `frames.polars`, `frames.pandas`, `frames.records` |
| [Errors](errors.md) | `NanoAPIError` and its subclasses |

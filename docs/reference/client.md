::: nanoapi.client
    options:
      members: false
      show_root_heading: true
      show_root_full_path: true
      heading_level: 1

::: nanoapi.Client
    options:
      show_bases: false  # the shared base class is private

::: nanoapi.AsyncClient
    options:
      show_bases: false  # the shared base class is private

## Bound routes

What you get when you access a route on a client instance (`api.get_user`).

::: nanoapi.BoundRoute

::: nanoapi.BoundPaginatedRoute

::: nanoapi.AsyncBoundRoute

::: nanoapi.AsyncBoundPaginatedRoute

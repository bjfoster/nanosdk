::: nanoapi.client
    options:
      members: false
      show_root_heading: true
      show_root_full_path: true
      heading_level: 1

::: nanoapi.Client
    options:
      show_bases: false  # the shared base class is private
      members: [with_options, close, _prepare_request, _process_response, _raise_for_status, _retry_policy]

::: nanoapi.AsyncClient
    options:
      show_bases: false  # the shared base class is private
      members: [with_options, aclose, _prepare_request, _process_response, _raise_for_status, _retry_policy]

::: nanoapi.http_client

## Bound routes

What you get when you access a route on a client instance (`api.get_user`).

::: nanoapi.BoundRoute

::: nanoapi.BoundPaginatedRoute

::: nanoapi.AsyncBoundRoute

::: nanoapi.AsyncBoundPaginatedRoute

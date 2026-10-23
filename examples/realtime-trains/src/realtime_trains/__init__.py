"""Typed Python client for the Realtime Trains API (https://data.rtt.io).

```python
from realtime_trains import RealtimeTrains

with RealtimeTrains.from_env() as rtt:          # RTT_ACCESS_TOKEN or RTT_REFRESH_TOKEN
    for row in rtt.board(code="CLJ", filter_to="WAT"):
        print(f"{row.scheduled:%H:%M} {row.destination:<20} {row.status}")
```

Response models live in `realtime_trains.models` (generated from the OpenAPI spec);
`realtime_trains.testing` has a fake API for your own tests.
"""

from .auth import AccessToken, RefreshTokenAuth
from .board import BoardRow, departure_board
from .client import (
    DEFAULT_BASE_URL,
    AllocationsByClass,
    AllocationsByClassQuery,
    AllocationsByService,
    AllocationsByServiceQuery,
    ApiInfo,
    AsyncRealtimeTrains,
    LocationLineUp,
    LocationQuery,
    Locations,
    NetworkRailLocationLineUp,
    NetworkRailLocationQuery,
    NetworkRailServiceDetail,
    NetworkRailServiceQuery,
    RateLimit,
    RealtimeTrains,
    ServiceDetail,
    ServiceQuery,
    Stops,
)
from .errors import InvalidQuery, NotEntitled, NotFound, Unauthorized

__all__ = [
    "DEFAULT_BASE_URL",
    "AccessToken",
    "AllocationsByClass",
    "AllocationsByClassQuery",
    "AllocationsByService",
    "AllocationsByServiceQuery",
    "ApiInfo",
    "AsyncRealtimeTrains",
    "BoardRow",
    "InvalidQuery",
    "LocationLineUp",
    "LocationQuery",
    "Locations",
    "NetworkRailLocationLineUp",
    "NetworkRailLocationQuery",
    "NetworkRailServiceDetail",
    "NetworkRailServiceQuery",
    "NotEntitled",
    "NotFound",
    "RateLimit",
    "RealtimeTrains",
    "RefreshTokenAuth",
    "ServiceDetail",
    "ServiceQuery",
    "Stops",
    "Unauthorized",
    "departure_board",
]

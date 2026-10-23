"""A flat, departure-board view of a location line-up.

The API's line-up is deeply nested (temporal data per activity, planned/actual
platforms, origin and destination lists, ...). `departure_board` boils each service
down to one [`BoardRow`][realtime_trains.board.BoardRow]: what a station screen shows.
It's also the shape you want for a DataFrame:

```python
rows = rtt.board(code="CLJ")                      # list[BoardRow]
df = nanoapi.frames.polars(rows)                  # needs realtime-trains[polars]
```
"""

from datetime import datetime

from pydantic import BaseModel

from ._generated.models import (
    IndividualTemporalData,
    LocationDisplayAs,
    LocationLineUpObject,
    LocationPair,
    LocationResponse,
    PlannedActualData,
)

__all__ = ["BoardRow", "departure_board"]


class BoardRow(BaseModel, frozen=True):
    """One service on a departure board.

    Attributes:
        service: The service's unique identity, for `rtt.service(unique_identity=...)`.
        operator: Train operator name.
        origin: Where the service starts.
        destination: Where it's going.
        scheduled: Advertised departure (arrival, for services terminating here).
        expected: Actual, forecast or estimated time, whichever is best known.
        platform: Actual platform if known, else the planned one.
        delay: Minutes late (negative when early); `None` without real-time data.
        status: Board text: `"On time"`, `"Exp 14:07"`, `"Departed"`, `"Cancelled"`,
            `"No report"` or `"Scheduled"`.
    """

    service: str | None
    operator: str | None
    origin: str
    destination: str
    scheduled: datetime | None
    expected: datetime | None
    platform: str | None
    delay: int | None
    status: str


def departure_board(lineup: LocationResponse) -> list[BoardRow]:
    """Flatten a location line-up into board rows, in the API's order.

    Use it directly, or through the `board` route, which is `location.map(departure_board)`.

    Args:
        lineup: A location line-up response.

    Returns:
        One row per service.
    """
    return [_row(service) for service in lineup.services or []]


def _row(service: LocationLineUpObject) -> BoardRow:
    temporal = service.temporal_data
    event = (temporal.departure or temporal.arrival) if temporal else None
    meta = service.schedule_metadata
    cancelled = bool(
        (event and event.is_cancelled)
        or (temporal and temporal.display_as == LocationDisplayAs.CANCELLED)
    )
    return BoardRow(
        service=meta.unique_identity if meta else None,
        operator=meta.operator.name if meta and meta.operator else None,
        origin=_places(service.origin),
        destination=_places(service.destination),
        scheduled=event.schedule_advertised if event else None,
        expected=_expected(event),
        platform=_platform(
            service.location_metadata.platform if service.location_metadata else None
        ),
        delay=event.realtime_advertised_lateness if event else None,
        status=_status(event, cancelled),
    )


def _places(pairs: list[LocationPair] | None) -> str:
    names = [p.location.description for p in pairs or [] if p.location and p.location.description]
    return " & ".join(names) or "?"


def _expected(event: IndividualTemporalData | None) -> datetime | None:
    if event is None:
        return None
    return event.realtime_actual or event.realtime_forecast or event.realtime_estimate


def _platform(platform: PlannedActualData | None) -> str | None:
    if platform is None:
        return None
    return platform.actual or platform.planned


def _status(event: IndividualTemporalData | None, cancelled: bool) -> str:
    if cancelled:
        return "Cancelled"
    if event is None:
        return "Scheduled"
    if event.realtime_actual is not None:
        return "Departed"
    if event.realtime_no_report:
        return "No report"
    expected = _expected(event)
    if expected is None or event.schedule_advertised is None:
        return "Scheduled"
    if expected > event.schedule_advertised:
        return f"Exp {expected:%H:%M}"
    return "On time"

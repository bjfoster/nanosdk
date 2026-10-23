"""An in-memory fake of the Realtime Trains API, for tests and demos.

It serves a small South Western Railway timetable around Clapham Junction, with times
relative to "now", some delays, a cancellation and a departed train. Responses are
built from the generated models, so they always match the spec's shapes. It checks
bearer tokens, implements the refresh-token exchange and sends rate-limit headers.

```python
from realtime_trains import RealtimeTrains
from realtime_trains.testing import FakeRealtimeTrains

fake = FakeRealtimeTrains()
rtt = RealtimeTrains(access_token=fake.access_token, transport=fake.transport())
rtt.board(code="CLJ")
```
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from . import models as m

__all__ = ["STATIONS", "FakeRealtimeTrains"]

STATIONS: dict[str, tuple[str, str]] = {
    "WAT": ("London Waterloo", "WATRLMN"),
    "VXH": ("Vauxhall", "VAUXHLM"),
    "CLJ": ("Clapham Junction", "CLPHMJN"),
    "WIM": ("Wimbledon", "WDON"),
    "SUR": ("Surbiton", "SURBITN"),
    "WOK": ("Woking", "WOKING"),
    "GLD": ("Guildford", "GUILDFD"),
    "BSK": ("Basingstoke", "BSNGSTK"),
    "RMD": ("Richmond", "RICHMND"),
    "TWI": ("Twickenham", "TWCKNHM"),
    "RDG": ("Reading", "RDNGSTN"),
    "BEU": ("Beaulieu Road", "BEAULRD"),  # a real station with no trains here: 204
}
"""Known stations: CRS code -> (name, TIPLOC)."""

SW = m.ScheduleMetadataOperator(code="SW", name="South Western Railway")
UP = ["WAT", "VXH", "CLJ", "WIM", "SUR", "WOK", "GLD"]
FAST = ["WAT", "CLJ", "WOK", "BSK"]
READING = ["WAT", "VXH", "CLJ", "RMD", "TWI", "RDG"]


@dataclass(frozen=True)
class _Train:
    identity: str
    headcode: str
    calls: list[str]
    start: int  # minutes after "now" that it leaves its origin
    delay: int = 0
    cancelled: bool = False
    interval: int = 4  # minutes between calls

    def times(self, base: datetime) -> list[tuple[str, datetime]]:
        first = base + timedelta(minutes=self.start)
        return [
            (code, first + timedelta(minutes=i * self.interval))
            for i, code in enumerate(self.calls)
        ]


TIMETABLE = [
    _Train("W10012", "2D05", UP, start=-14),
    _Train("W10049", "1L40", FAST, start=-4, interval=7),
    _Train("W10086", "2R47", READING, start=-2, delay=4),
    _Train("W10123", "2G11", UP[::-1], start=-12, delay=2),
    _Train("W10160", "2D09", UP, start=6, cancelled=True),
    _Train("W10197", "1L44", FAST, start=12, interval=7),
    _Train("W10234", "2R51", READING, start=17, delay=11),
    _Train("W10271", "2G15", UP[::-1], start=8),
    _Train("W10308", "2D13", UP, start=26),
]


class FakeRealtimeTrains:
    """A fake `data.rtt.io`: pass `transport()` (or `async_transport()`) to a client.

    Args:
        now: The fake's current time; defaults to the real time, to the minute.
        access_token: A long-life access token the fake accepts.
        refresh_token: A refresh token the fake exchanges at `/api/get_access_token`.
        token_lifetime: How long exchanged access tokens are valid.

    Attributes:
        requests: Every request received, in order.
        issued: Access tokens issued by the token endpoint.
        revoked: Tokens the fake now rejects; add one to simulate revocation.
    """

    def __init__(
        self,
        *,
        now: datetime | None = None,
        access_token: str = "demo-access-token",
        refresh_token: str = "demo-refresh-token",
        token_lifetime: timedelta = timedelta(hours=1),
    ) -> None:
        self.now = (now or datetime.now(UTC)).replace(second=0, microsecond=0)
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_lifetime = token_lifetime
        self.requests: list[httpx.Request] = []
        self.issued: list[str] = []
        self.revoked: set[str] = set()
        self._remaining = 100

    def transport(self) -> httpx.MockTransport:
        """A transport for `RealtimeTrains(transport=...)`."""
        return httpx.MockTransport(self)

    def async_transport(self) -> httpx.MockTransport:
        """A transport for `AsyncRealtimeTrains(transport=...)`."""
        return httpx.MockTransport(self)

    # -- request handling -------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Handle one request."""
        self.requests.append(request)
        self._remaining = max(self._remaining - 1, 0)
        response = self._route(request)
        response.headers["X-RateLimit-Limit-Minute"] = "100"
        response.headers["X-RateLimit-Remaining-Minute"] = str(self._remaining)
        return response

    def _route(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        path, q = request.url.path, request.url.params
        if path == "/api/get_access_token":
            return self._exchange(token)
        if token in self.revoked or token not in {self.access_token, *self.issued}:
            return _json(401, {"error": "invalid or expired token"})
        match path:
            case "/rtt/location" | "/gb-nr/location":
                return self._location(q, network_rail=path.startswith("/gb-nr"))
            case "/rtt/service" | "/gb-nr/service":
                return self._service(q, network_rail=path.startswith("/gb-nr"))
            case "/data/stops":
                return _model(m.DataStopsGetResponse(stops=[_stop(c) for c in STATIONS]))
            case "/data/locations_ungrouped":
                return _model(
                    m.DataLocationsUngroupedGetResponse(locations=[_place(c) for c in STATIONS])
                )
            case "/gb-nr/allocations/by-service" | "/gb-nr/allocations/by-class":
                return _json(403, {"error": "requires allowFullAllocationListing"})
            case "/api/info":
                credentials = m.ApiInfoGetResponseCredentials(
                    entitlements=["allowDetailed"], history_restriction=True,
                    history_restrict_to_days=14, namespace_restriction=False,
                )  # fmt: skip
                return _model(
                    m.ApiInfoGetResponse(api_version="2026-01-18", credentials=credentials)
                )
            case _:
                return _json(404, {"error": f"no route {path}"})

    def _exchange(self, refresh_token: str) -> httpx.Response:
        if refresh_token != self.refresh_token:
            return _json(401, {"error": "invalid refresh token"})
        token = f"issued-{len(self.issued) + 1}"
        self.issued.append(token)
        return _model(
            m.ApiGetAccessTokenGetResponse(
                token=token,
                entitlements=["allowDetailed"],
                # Tokens expire by the wall clock, even when the timetable's `now` is fixed.
                valid_until=datetime.now(UTC) + self.token_lifetime,
            )
        )

    def _location(self, q: httpx.QueryParams, *, network_rail: bool) -> httpx.Response:
        code = q.get("code", "").upper()
        if code not in STATIONS:
            return _json(400, {"error": f"unknown location {code!r}"})
        start = _parse_time(q.get("timeFrom")) or self.now
        end = _parse_time(q.get("timeTo")) or start + timedelta(
            minutes=int(q.get("timeWindow", 60))
        )
        services: list[dict[str, Any]] = []
        for train in TIMETABLE:
            times = dict(train.times(self.now))
            order = train.calls
            if code not in times or not start <= times[code] <= end:
                continue
            here = order.index(code)
            if (to := q.get("filterTo")) and to.upper() not in order[here + 1 :]:
                continue
            if (frm := q.get("filterFrom")) and frm.upper() not in order[:here]:
                continue
            services.append(self._lineup_entry(train, code, network_rail))
        if not services:
            return httpx.Response(204)
        services.sort(key=_board_time)
        query = {
            "location": _place_model(code).model_dump(
                mode="json", by_alias=True, exclude_none=True
            ),
            "timeFrom": start.isoformat(),
            "timeTo": end.isoformat(),
        }
        return _json(200, {"systemStatus": {"realtimeNetworkRail": "OK", "rttCore": "OK"},
                           "query": query, "services": services})  # fmt: skip

    def _lineup_entry(self, train: _Train, code: str, network_rail: bool) -> dict[str, Any]:
        times = train.times(self.now)
        when = dict(times)[code]
        terminates = code == train.calls[-1]
        event = self._event(train, when)
        temporal = m.LocationTemporalData(
            arrival=event if terminates else None,
            departure=None if terminates else event,
            display_as=m.LocationDisplayAs.CANCELLED
            if train.cancelled
            else m.LocationDisplayAs.CALL,
        )
        platform = m.PlannedActualData(planned=str(1 + int(train.identity[-2:]) % 9))
        entry_type = m.NetworkRailLocationLineUpObject if network_rail else m.LocationLineUpObject
        entry = entry_type.model_validate({
            "temporal_data": temporal,
            "location_metadata": {"platform": platform, "number_of_vehicles": 8},
            "origin": [self._pair(train, train.calls[0])],
            "destination": [self._pair(train, train.calls[-1])],
            "schedule_metadata": self._schedule(train, network_rail),
        })  # fmt: skip
        return entry.model_dump(mode="json", by_alias=True, exclude_none=True)

    def _service(self, q: httpx.QueryParams, *, network_rail: bool) -> httpx.Response:
        wanted = q.get("uniqueIdentity") or f"gb-nr:{q.get('identity')}:{q.get('departureDate')}"
        train = next((t for t in TIMETABLE if self._unique(t) == wanted), None)
        if train is None:
            return _json(404, {"error": "service not found"})
        locations = [
            {
                "location": _place_model(code),
                "temporal_data": {
                    "arrival": None if i == 0 else self._event(train, when),
                    "departure": None if i == len(train.calls) - 1 else self._event(train, when),
                    "display_as": "CANCELLED" if train.cancelled else "CALL",
                },
            }
            for i, (code, when) in enumerate(train.times(self.now))
        ]
        body_type = m.GbNrServiceGetResponse if network_rail else m.RttServiceGetResponse
        body = body_type.model_validate({
            "query": {"unique_identity": wanted},
            "service": {
                "schedule_metadata": self._schedule(train, network_rail),
                "locations": locations,
                "origin": [self._pair(train, train.calls[0])],
                "destination": [self._pair(train, train.calls[-1])],
            },
        })  # fmt: skip
        return _model(body)

    # -- data builders --------------------------------------------------------------

    def _unique(self, train: _Train) -> str:
        return f"gb-nr:{train.identity}:{self._service_date(train).isoformat()}"

    def _service_date(self, train: _Train) -> date:
        return train.times(self.now)[0][1].date()

    def _schedule(self, train: _Train, network_rail: bool) -> dict[str, Any]:
        schedule: dict[str, Any] = {
            "unique_identity": self._unique(train),
            "namespace": "gb-nr",
            "identity": train.identity,
            "departure_date": self._service_date(train),
            "operator": SW,
            "mode_type": "TRAIN",
            "in_passenger_service": True,
        }
        if network_rail:
            schedule |= {"train_reporting_identity": train.headcode, "stp_indicator": "WTT"}
        return schedule

    def _event(self, train: _Train, scheduled: datetime) -> m.IndividualTemporalData:
        if train.cancelled:
            return m.IndividualTemporalData(schedule_advertised=scheduled, is_cancelled=True)
        real = scheduled + timedelta(minutes=train.delay)
        happened = real <= self.now
        return m.IndividualTemporalData(
            schedule_advertised=scheduled,
            realtime_actual=real if happened else None,
            realtime_forecast=None if happened else real,
            realtime_advertised_lateness=train.delay,
            is_cancelled=False,
        )

    def _pair(self, train: _Train, code: str) -> m.LocationPair:
        when = dict(train.times(self.now))[code]
        return m.LocationPair(
            location=_place_model(code),
            temporal_data=m.IndividualTemporalData(schedule_advertised=when),
        )


def _place_model(code: str) -> m.GeographicLocation:
    name, tiploc = STATIONS[code]
    return m.GeographicLocation(description=name, short_codes=[code], long_codes=[tiploc])


def _stop(code: str) -> m.DataStopsGetResponseStop:
    return m.DataStopsGetResponseStop(
        description=STATIONS[code][0], short_code=code, unique_identity=f"gb-nr:{code}"
    )


def _place(code: str) -> m.DataLocationsUngroupedGetResponseLocation:
    name, tiploc = STATIONS[code]
    return m.DataLocationsUngroupedGetResponseLocation(
        description=name, short_code=code, long_code=tiploc, unique_identity=f"gb-nr:{tiploc}"
    )


def _board_time(entry: dict[str, Any]) -> str:
    temporal: dict[str, Any] = entry.get("temporalData", {})
    event: dict[str, Any] = temporal.get("departure") or temporal.get("arrival") or {}
    return str(event.get("scheduleAdvertised", ""))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


def _model(model: BaseModel) -> httpx.Response:
    return _json(200, model.model_dump(mode="json", by_alias=True, exclude_none=True))

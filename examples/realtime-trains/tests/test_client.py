from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import polars as pl
import pytest
from pydantic import ValidationError

import nanoapi
from realtime_trains import (
    AsyncRealtimeTrains,
    InvalidQuery,
    NotEntitled,
    NotFound,
    RealtimeTrains,
    RefreshTokenAuth,
    Unauthorized,
)
from realtime_trains.__main__ import main
from realtime_trains.testing import TIMETABLE, FakeRealtimeTrains

NOW = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)


@pytest.fixture
def fake() -> FakeRealtimeTrains:
    return FakeRealtimeTrains(now=NOW)


@pytest.fixture
def rtt(fake: FakeRealtimeTrains) -> RealtimeTrains:
    return RealtimeTrains(access_token=fake.access_token, transport=fake.transport())


def paths(fake: FakeRealtimeTrains) -> list[str]:
    return [f"{r.url.path}?{r.url.query.decode()}".rstrip("?") for r in fake.requests]


# --- the wire ---------------------------------------------------------------------


def test_query_params_are_camel_case_and_iso(rtt: RealtimeTrains, fake: FakeRealtimeTrains) -> None:
    rtt.location(code="CLJ", filter_to="WAT", time_from=NOW, time_window=90, detailed=True)
    with pytest.raises(NotEntitled):  # the fake's token lacks the allocation entitlements
        rtt.nr_allocations_by_class(departure_date=date(2026, 3, 2), vehicle_class="444")

    assert paths(fake) == [
        "/rtt/location?code=CLJ&filterTo=WAT&timeFrom=2026-03-02T08%3A00%3A00Z"
        "&timeWindow=90&detailed=true",
        "/gb-nr/allocations/by-class?departureDate=2026-03-02&class=444",
    ]
    assert fake.requests[0].headers["authorization"] == "Bearer demo-access-token"


def test_api_version_header(fake: FakeRealtimeTrains) -> None:
    rtt = RealtimeTrains(
        access_token=fake.access_token, api_version=date(2025, 10, 25), transport=fake.transport()
    )
    rtt.info()
    assert fake.requests[0].headers["version"] == "2025-10-25"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"code": "CLJ", "time_from": NOW, "time_to": NOW + timedelta(hours=1), "time_window": 60},
        {"code": "CLJ", "time_from": NOW, "time_to": NOW + timedelta(hours=24)},
        {"code": "CLJ", "time_from": NOW, "time_to": NOW - timedelta(minutes=5)},
        {"code": "CLJ", "time_window": 0},
    ],
)
def test_documented_constraints_are_checked_before_sending(
    rtt: RealtimeTrains, fake: FakeRealtimeTrains, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        rtt.location(**kwargs)
    assert fake.requests == []


def test_network_rail_specific_checks(rtt: RealtimeTrains, fake: FakeRealtimeTrains) -> None:
    with pytest.raises(ValidationError, match="detailed"):
        rtt.nr_location(code="CLJ", stp_filter="WV")
    with pytest.raises(ValidationError, match="pattern"):
        rtt.nr_location(code="CLJ", stp_filter="X", detailed=True)
    with pytest.raises(ValidationError, match="unique_identity"):
        rtt.nr_service(identity="W10049")  # needs departure_date too
    with pytest.raises(ValidationError, match="unique_identity"):
        rtt.service(unique_identity="gb-nr:W1:2026-03-02", identity="W1", departure_date=NOW.date())
    assert fake.requests == []


# --- behaviour ------------------------------------------------------------------------


def test_board(rtt: RealtimeTrains) -> None:
    rows = rtt.board(code="CLJ")

    times = [row.scheduled for row in rows if row.scheduled is not None]
    assert len(times) == len(rows)
    assert times == sorted(times)
    statuses = {row.status for row in rows}
    assert {"On time", "Cancelled"} <= statuses
    assert any(status.startswith("Exp ") for status in statuses)
    reading = next(row for row in rows if row.destination == "Reading" and row.delay == 4)
    assert reading.scheduled is not None
    assert reading.expected == reading.scheduled + timedelta(minutes=4)
    assert reading.platform is not None
    assert reading.operator == "South Western Railway"

    to_waterloo = rtt.board(code="CLJ", filter_to="WAT")
    assert to_waterloo
    assert {row.destination for row in to_waterloo} == {"London Waterloo"}


def test_no_services_is_an_empty_line_up_not_an_error(rtt: RealtimeTrains) -> None:
    assert rtt.location(code="BEU").services == []
    assert rtt.board(code="BEU") == []


def test_errors_map_to_exceptions(rtt: RealtimeTrains, fake: FakeRealtimeTrains) -> None:
    with pytest.raises(InvalidQuery) as invalid:
        rtt.location(code="XYZ")
    assert invalid.value.body == {"error": "unknown location 'XYZ'"}
    with pytest.raises(NotFound):
        rtt.service(unique_identity="gb-nr:NOPE:2026-03-02")
    with pytest.raises(nanoapi.APIStatusError):  # the base class still catches them
        rtt.service(identity="NOPE", departure_date=NOW.date())

    bad = RealtimeTrains(access_token="wrong", transport=fake.transport())
    with pytest.raises(Unauthorized):
        bad.info()


def test_rate_limits_are_recorded(rtt: RealtimeTrains) -> None:
    rtt.stops()
    rtt.stops()
    minute = rtt.rate_limits["minute"]
    assert (minute.limit, minute.remaining) == (100, 98)


def test_network_rail_detail(rtt: RealtimeTrains) -> None:
    lineup = rtt.nr_location(code="WOK")
    headcodes = {
        s.schedule_metadata.train_reporting_identity
        for s in lineup.services or []
        if s.schedule_metadata
    }
    assert {"2D05", "1L40"} <= headcodes

    fast = next(t for t in TIMETABLE if t.headcode == "1L40")
    detail = rtt.nr_service(identity=fast.identity, departure_date=NOW.date()).service
    assert detail is not None
    assert [stop.location.description for stop in detail.locations or [] if stop.location] == [
        "London Waterloo",
        "Clapham Junction",
        "Woking",
        "Basingstoke",
    ]


def test_retries_rate_limiting(fake: FakeRealtimeTrains) -> None:
    calls = 0

    def busy_once(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return fake(request)

    rtt = RealtimeTrains(access_token=fake.access_token, transport=httpx.MockTransport(busy_once))
    assert rtt.stops().stops
    assert calls == 2


def test_board_to_dataframe(rtt: RealtimeTrains) -> None:
    df = nanoapi.frames.polars(rtt.board(code="CLJ"))
    assert isinstance(df, pl.DataFrame)
    assert {"scheduled", "destination", "status", "delay"} <= set(df.columns)


# --- auth --------------------------------------------------------------------------------


def refreshing(fake: FakeRealtimeTrains) -> RealtimeTrains:
    return RealtimeTrains(refresh_token=fake.refresh_token, transport=fake.transport())


def test_refresh_token_is_exchanged_once_and_reused(fake: FakeRealtimeTrains) -> None:
    rtt = refreshing(fake)
    rtt.info()
    rtt.stops()

    assert paths(fake) == ["/api/get_access_token", "/api/info", "/data/stops"]
    assert fake.requests[0].headers["authorization"] == "Bearer demo-refresh-token"
    assert fake.requests[1].headers["authorization"] == "Bearer issued-1"


def test_short_lived_tokens_are_renewed_before_they_expire() -> None:
    fake = FakeRealtimeTrains(token_lifetime=timedelta(seconds=30))  # < the 1 min leeway
    rtt = refreshing(fake)
    rtt.info()
    rtt.info()
    assert fake.issued == ["issued-1", "issued-2"]


def test_revoked_token_is_replaced_and_the_request_retried(fake: FakeRealtimeTrains) -> None:
    rtt = refreshing(fake)
    rtt.info()
    fake.revoked.add("issued-1")

    assert rtt.info().api_version == "2026-01-18"
    assert fake.issued == ["issued-1", "issued-2"]


def test_bad_refresh_token(fake: FakeRealtimeTrains) -> None:
    rtt = RealtimeTrains(refresh_token="nope", transport=fake.transport())
    with pytest.raises(nanoapi.APIStatusError) as info:
        rtt.info()
    assert info.value.request.url.path == "/api/get_access_token"


def test_exactly_one_token(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        RealtimeTrains()
    with pytest.raises(ValueError, match="exactly one"):
        RealtimeTrains(access_token="a", refresh_token="r")

    monkeypatch.delenv("RTT_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("RTT_REFRESH_TOKEN", "from-env")
    assert isinstance(nanoapi.http_client(RealtimeTrains.from_env()).auth, RefreshTokenAuth)


# --- async and CLI -------------------------------------------------------------------


async def test_async_client(fake: FakeRealtimeTrains) -> None:
    async with AsyncRealtimeTrains(
        refresh_token=fake.refresh_token, transport=fake.async_transport()
    ) as rtt:
        rows = await rtt.board(code="WAT")
        assert rows
        assert rtt.rate_limits["minute"].remaining is not None
        assert (await rtt.location(code="BEU")).services == []
    assert fake.issued == ["issued-1"]


def test_cli_demo(capsys: pytest.CaptureFixture[str]) -> None:
    main(["--demo", "board", "CLJ", "--to", "WAT"])
    out = capsys.readouterr().out
    assert "London Waterloo" in out
    assert "requests left this minute" in out


@pytest.mark.parametrize("make", [lambda: main(["--demo", "board", "XYZ"])])
def test_cli_reports_api_errors(make: Callable[[], None]) -> None:
    with pytest.raises(SystemExit, match="InvalidQuery: 400"):
        make()

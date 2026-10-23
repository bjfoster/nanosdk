"""Request models, routes and the sync/async clients for the Realtime Trains API.

The response models are generated from the OpenAPI spec (`models.py`); this module adds
what codegen can't: the query parameters of each operation (with the spec's documented
constraints as validation), friendly names for the per-endpoint response models, and
the API's conventions for auth, versions, rate limits, empty results and errors.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

import nanoapi

from . import models
from .auth import RefreshTokenAuth
from .board import departure_board
from .errors import BY_STATUS

__all__ = [
    "DEFAULT_BASE_URL",
    "AllocationsByClass",
    "AllocationsByClassQuery",
    "AllocationsByService",
    "AllocationsByServiceQuery",
    "ApiInfo",
    "AsyncRealtimeTrains",
    "LocationLineUp",
    "LocationQuery",
    "Locations",
    "NetworkRailLocationLineUp",
    "NetworkRailLocationQuery",
    "NetworkRailServiceDetail",
    "NetworkRailServiceQuery",
    "RateLimit",
    "RealtimeTrains",
    "ServiceDetail",
    "ServiceQuery",
    "Stops",
]

DEFAULT_BASE_URL = "https://data.rtt.io"

# Friendlier names for the response models codegen named after their paths.
LocationLineUp = models.RttLocationGetResponse
ServiceDetail = models.RttServiceGetResponse
NetworkRailLocationLineUp = models.GbNrLocationGetResponse
NetworkRailServiceDetail = models.GbNrServiceGetResponse
AllocationsByService = models.GbNrAllocationsByServiceGetResponse1  # /allocations/by-service
AllocationsByClass = models.GbNrAllocationsByClassGetResponse
Locations = models.DataLocationsUngroupedGetResponse
Stops = models.DataStopsGetResponse
ApiInfo = models.ApiInfoGetResponse

MAX_WINDOW = timedelta(hours=23, minutes=59)


# --- query parameters -------------------------------------------------------------


class _Params(BaseModel):
    """Base for query models: snake_case in Python, camelCase on the wire."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class LocationQuery(_Params):
    """Parameters for a location line-up (the services at a station).

    Args:
        code: Short (CRS, `"CLJ"`) or long (TIPLOC, `"CLPHMJN"`) location code.
        filter_from: Only services that previously called at this location.
        filter_to: Only services that subsequently call at this location.
        time_from: Start of the window; defaults to now. Naive datetimes are taken as
            the location's local time.
        time_to: End of the window; defaults to 60 minutes after `time_from`.
        time_window: Window length in minutes, instead of `time_to`.
        time_tolerance: Start the window slightly before `time_from`.
        detailed: Detailed mode (internal times); needs the `allowDetailed` entitlement.
    """

    code: str
    filter_from: str | None = None
    filter_to: str | None = None
    time_from: datetime | None = None
    time_to: datetime | None = None
    time_window: Annotated[int | None, Field(gt=0, le=MAX_WINDOW.seconds // 60)] = None
    time_tolerance: bool | None = None
    detailed: bool | None = None

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.time_to is not None and self.time_window is not None:
            raise ValueError("time_to and time_window can't be combined")
        if self.time_from is not None and self.time_to is not None:
            if self.time_to <= self.time_from:
                raise ValueError("time_to must be after time_from")
            if self.time_to - self.time_from > MAX_WINDOW:
                raise ValueError("the maximum window is 23 hours 59 minutes")
        return self


class NetworkRailLocationQuery(LocationQuery):
    """Parameters for a Network Rail location line-up.

    Args:
        stp_filter: STP types to include, up to 4 of `W` (WTT), `V` (variation),
            `S` (short-term plan), `C` (cancellation). Needs `detailed=True`.
    """

    stp_filter: Annotated[str | None, Field(pattern=r"^[WVSC]{1,4}$")] = None

    @model_validator(mode="after")
    def _check_stp(self) -> Self:
        if self.stp_filter is not None and not self.detailed:
            raise ValueError("stp_filter only works with detailed=True")
        return self


class NetworkRailServiceQuery(_Params):
    """Identify a Network Rail service.

    Pass either `unique_identity` (`"gb-nr:L01525:2025-10-26"`) or both `identity` and
    `departure_date`.

    Args:
        unique_identity: Namespace, identity and departure date in one string.
        identity: The service's identity (`"L01525"`).
        departure_date: Departure date from its origin.
        detailed: Detailed mode; needs the `allowDetailed` entitlement.
    """

    unique_identity: str | None = None
    identity: str | None = None
    departure_date: date | None = None
    detailed: bool | None = None

    @model_validator(mode="after")
    def _check_identity(self) -> Self:
        by_parts = self.identity is not None and self.departure_date is not None
        if (self.unique_identity is not None) == by_parts:
            raise ValueError("pass unique_identity, or identity and departure_date")
        return self


class ServiceQuery(NetworkRailServiceQuery):
    """Identify a service in any namespace.

    Args:
        namespace: The namespace when not using `unique_identity` (API default `gb-nr`).
    """

    namespace: str | None = None


class AllocationsByServiceQuery(_Params):
    """Rolling-stock allocations for a train operator's services on a date.

    Args:
        departure_date: Departure date from origin.
        toc: Train operator code, such as `"SW"`.
    """

    departure_date: date
    toc: str


class AllocationsByClassQuery(_Params):
    """Rolling-stock allocations for a class of vehicle on a date.

    Args:
        departure_date: Departure date from origin.
        vehicle_class: Rolling-stock class, such as `"444"` (sent as `class`).
    """

    departure_date: date
    vehicle_class: Annotated[str, nanoapi.Query(alias="class")]


# --- routes -------------------------------------------------------------------------

# 204 means "valid query, no services": return an empty line-up instead of failing.
_EMPTY_ON_204 = {"empty_on_204": True}


class GenericRoutes(nanoapi.Routes, prefix="/rtt"):
    """Namespace-independent endpoints."""

    location = nanoapi.get(
        "/location", request=LocationQuery, response=LocationLineUp, meta=_EMPTY_ON_204
    )
    board = location.map(departure_board)  # the same call, flattened to board rows
    service = nanoapi.get("/service", request=ServiceQuery, response=ServiceDetail)


class NetworkRailRoutes(nanoapi.Routes, prefix="/gb-nr"):
    """Network Rail (`gb-nr`) endpoints, with headcodes, STP data and allocations."""

    nr_location = nanoapi.get(
        "/location",
        request=NetworkRailLocationQuery,
        response=NetworkRailLocationLineUp,
        meta=_EMPTY_ON_204,
    )
    nr_service = nanoapi.get(
        "/service", request=NetworkRailServiceQuery, response=NetworkRailServiceDetail
    )
    nr_allocations_by_service = nanoapi.get(
        "/allocations/by-service", request=AllocationsByServiceQuery, response=AllocationsByService
    )
    nr_allocations_by_class = nanoapi.get(
        "/allocations/by-class", request=AllocationsByClassQuery, response=AllocationsByClass
    )


class ReferenceDataRoutes(nanoapi.Routes, prefix="/data"):
    """Reference data: the locations and stops the API knows about."""

    locations = nanoapi.get("/locations_ungrouped", response=Locations)
    stops = nanoapi.get("/stops", response=Stops)


class AccountRoutes(nanoapi.Routes, prefix="/api"):
    """Your API version, entitlements and restrictions."""

    info = nanoapi.get("/info", response=ApiInfo)


# --- client behaviour shared by the sync and async clients ------------------------------


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Your allowance in one rate-limit dimension, from the latest response.

    Attributes:
        limit: Requests allowed in the period.
        remaining: Requests left in the period.
    """

    limit: int | None
    remaining: int | None


def _client_options(
    base_url: str,
    access_token: str | None,
    refresh_token: str | None,
    api_version: str | date | None,
) -> dict[str, Any]:
    if (access_token is None) == (refresh_token is None):
        raise ValueError("pass exactly one of access_token or refresh_token")
    auth = (
        nanoapi.auth.Bearer(access_token)
        if access_token is not None
        else RefreshTokenAuth(
            refresh_token or "", token_url=f"{base_url.rstrip('/')}/api/get_access_token"
        )
    )
    headers = {"Version": str(api_version)} if api_version is not None else {}
    return {"auth": auth, "headers": headers}


def _tokens_from_env() -> tuple[str | None, str | None]:
    """`(access_token, refresh_token)` from the environment; the access token wins."""
    if access := os.environ.get("RTT_ACCESS_TOKEN"):
        return access, None
    if refresh := os.environ.get("RTT_REFRESH_TOKEN"):
        return None, refresh
    raise ValueError("set RTT_ACCESS_TOKEN or RTT_REFRESH_TOKEN")


def _record_rate_limits(store: dict[str, RateLimit], response: httpx.Response) -> None:
    for period in ("Minute", "Hour", "Day", "Week"):
        limit = response.headers.get(f"X-RateLimit-Limit-{period}")
        remaining = response.headers.get(f"X-RateLimit-Remaining-{period}")
        if limit is not None or remaining is not None:
            store[period.lower()] = RateLimit(
                int(limit) if limit else None, int(remaining) if remaining else None
            )


def _process(
    store: dict[str, RateLimit], response: httpx.Response, route: nanoapi.Route[Any, Any, Any]
) -> httpx.Response:
    _record_rate_limits(store, response)
    if response.status_code == 204 and route.meta.get("empty_on_204"):
        return httpx.Response(200, json={"services": []}, request=response.request)
    return response


def _raise_for_status(response: httpx.Response) -> None:
    if not response.is_error:
        return
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    raise BY_STATUS.get(response.status_code, nanoapi.APIStatusError)(response, body=body)


# --- clients ----------------------------------------------------------------------------


class RealtimeTrains(
    GenericRoutes, NetworkRailRoutes, ReferenceDataRoutes, AccountRoutes, nanoapi.Client
):
    """Synchronous Realtime Trains client.

    Example:
        ```python
        with RealtimeTrains.from_env() as rtt:
            for row in rtt.board(code="CLJ", filter_to="WAT"):
                print(row.scheduled, row.destination, row.status)
        ```

    Retries 429s (honouring `Retry-After`) and transient 5xx errors by default. A
    location query with no matching services returns an empty line-up rather than
    raising, and errors raise the exceptions in [`realtime_trains.errors`][realtime_trains.errors].

    Args:
        access_token: A long-life access token. Pass this or `refresh_token`.
        refresh_token: A refresh token, exchanged for access tokens as needed.
        api_version: Pin the API version (`"2025-10-25"`); latest if omitted.
        base_url: The API's base URL.
        retry: Retry policy; `None` disables retries.
        transport: An httpx transport; tests pass an `httpx.MockTransport`.

    Raises:
        ValueError: Unless exactly one of `access_token` and `refresh_token` is given.
    """

    def __init__(
        self,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        api_version: str | date | None = None,
        base_url: str = DEFAULT_BASE_URL,
        retry: nanoapi.Retry | None = nanoapi.Retry(),  # noqa: B008 (immutable)
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        options = _client_options(base_url, access_token, refresh_token, api_version)
        super().__init__(base_url, retry=retry, transport=transport, **options)
        self._rate_limits: dict[str, RateLimit] = {}

    @classmethod
    def from_env(cls, **kwargs: Any) -> Self:
        """Create a client from `RTT_ACCESS_TOKEN` or `RTT_REFRESH_TOKEN`.

        Args:
            **kwargs: Other constructor arguments.

        Raises:
            ValueError: If neither variable is set.
        """
        access, refresh = _tokens_from_env()
        return cls(access_token=access, refresh_token=refresh, **kwargs)

    @property
    def rate_limits(self) -> Mapping[str, RateLimit]:
        """Your remaining allowance per period (`"minute"`, `"hour"`, ...).

        Updated from the rate-limit headers of every response.
        """
        return self._rate_limits

    def process_response(
        self, response: httpx.Response, route: nanoapi.Route[Any, Any, Any]
    ) -> httpx.Response:
        """Record rate limits; turn "no services" (204) into an empty line-up."""
        return _process(self._rate_limits, response, route)

    def raise_for_status(self, response: httpx.Response) -> None:
        """Raise the matching [`realtime_trains.errors`][realtime_trains.errors] exception."""
        _raise_for_status(response)


class AsyncRealtimeTrains(
    GenericRoutes, NetworkRailRoutes, ReferenceDataRoutes, AccountRoutes, nanoapi.AsyncClient
):
    """Asynchronous Realtime Trains client; the async twin of `RealtimeTrains`.

    Args:
        access_token: A long-life access token. Pass this or `refresh_token`.
        refresh_token: A refresh token, exchanged for access tokens as needed.
        api_version: Pin the API version; latest if omitted.
        base_url: The API's base URL.
        retry: Retry policy; `None` disables retries.
        transport: An async httpx transport.

    Raises:
        ValueError: Unless exactly one of `access_token` and `refresh_token` is given.
    """

    def __init__(
        self,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        api_version: str | date | None = None,
        base_url: str = DEFAULT_BASE_URL,
        retry: nanoapi.Retry | None = nanoapi.Retry(),  # noqa: B008 (immutable)
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        options = _client_options(base_url, access_token, refresh_token, api_version)
        super().__init__(base_url, retry=retry, transport=transport, **options)
        self._rate_limits: dict[str, RateLimit] = {}

    @classmethod
    def from_env(cls, **kwargs: Any) -> Self:
        """Create a client from `RTT_ACCESS_TOKEN` or `RTT_REFRESH_TOKEN`.

        Args:
            **kwargs: Other constructor arguments.

        Raises:
            ValueError: If neither variable is set.
        """
        access, refresh = _tokens_from_env()
        return cls(access_token=access, refresh_token=refresh, **kwargs)

    @property
    def rate_limits(self) -> Mapping[str, RateLimit]:
        """Your remaining allowance per period, as of the latest response."""
        return self._rate_limits

    async def process_response(
        self, response: httpx.Response, route: nanoapi.Route[Any, Any, Any]
    ) -> httpx.Response:
        """Record rate limits; turn "no services" (204) into an empty line-up."""
        return _process(self._rate_limits, response, route)

    def raise_for_status(self, response: httpx.Response) -> None:
        """Raise the matching [`realtime_trains.errors`][realtime_trains.errors] exception."""
        _raise_for_status(response)

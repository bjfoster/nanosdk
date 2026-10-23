"""Command line: departure boards, service details and account info.

rtt board CLJ --to WAT          # needs RTT_ACCESS_TOKEN or RTT_REFRESH_TOKEN
rtt service gb-nr:L01525:2025-10-26
rtt info
rtt --demo board CLJ            # offline, against realtime_trains.testing
"""

import argparse
from datetime import datetime

import nanoapi

from .client import RealtimeTrains
from .models import IndividualTemporalData
from .testing import FakeRealtimeTrains

__all__ = ["main"]


def main(argv: list[str] | None = None) -> None:
    """Run the `rtt` command."""
    parser = argparse.ArgumentParser(prog="rtt", description="Realtime Trains from the terminal.")
    parser.add_argument("--demo", action="store_true", help="use the offline fake API")
    commands = parser.add_subparsers(dest="command", required=True)
    board = commands.add_parser("board", help="departures from a station")
    board.add_argument("code", help="station code, such as CLJ")
    board.add_argument("--to", help="only trains calling here later")
    board.add_argument("--window", type=int, help="minutes ahead (default 60)")
    service = commands.add_parser("service", help="a service's calling points")
    service.add_argument("unique_identity", help="such as gb-nr:L01525:2025-10-26")
    commands.add_parser("info", help="API version and your entitlements")
    args = parser.parse_args(argv)

    if args.demo:
        fake = FakeRealtimeTrains()
        rtt = RealtimeTrains(access_token=fake.access_token, transport=fake.transport())
    else:
        try:
            rtt = RealtimeTrains.from_env()
        except ValueError as exc:
            parser.exit(2, f"rtt: {exc} (or try --demo)\n")

    with rtt:
        try:
            match args.command:
                case "board":
                    _board(rtt, args.code, args.to, args.window)
                case "service":
                    _service(rtt, args.unique_identity)
                case _:
                    info = rtt.info()
                    credentials = info.credentials
                    print(f"API version {info.api_version}")
                    print(
                        "entitlements:",
                        ", ".join(credentials.entitlements or []) if credentials else "-",
                    )
        except nanoapi.APIStatusError as exc:
            raise SystemExit(f"{type(exc).__name__}: {exc.status_code} {exc.body}") from exc
        limit = rtt.rate_limits.get("minute")
        if limit:
            print(f"\n{limit.remaining}/{limit.limit} requests left this minute")


def _board(rtt: RealtimeTrains, code: str, to: str | None, window: int | None) -> None:
    rows = rtt.board(code=code, filter_to=to, time_window=window)
    if not rows:
        print(f"No trains from {code}" + (f" to {to}" if to else ""))
        return
    print(f"{'Time':<6} {'Destination':<20} {'Plat':<5} {'Status':<11} Operator")
    for row in rows:
        print(
            f"{_hhmm(row.scheduled):<6} {row.destination[:20]:<20} {row.platform or '-':<5} "
            f"{row.status:<11} {row.operator or ''}"
        )


def _service(rtt: RealtimeTrains, unique_identity: str) -> None:
    detail = rtt.service(unique_identity=unique_identity).service
    if detail is None:
        raise SystemExit("no service data")
    meta = detail.schedule_metadata
    if meta and meta.operator:
        print(f"{meta.identity} on {meta.departure_date}, {meta.operator.name}\n")
    for stop in detail.locations or []:
        name = stop.location.description if stop.location else "?"
        times = stop.temporal_data
        arrive = _event(times.arrival if times else None)
        depart = _event(times.departure if times else None)
        print(f"  {name:<20} arr {arrive:<18} dep {depart}")


def _event(event: IndividualTemporalData | None) -> str:
    if event is None:
        return "-"
    if event.is_cancelled:
        return f"{_hhmm(event.schedule_advertised)} cancelled"
    real = event.realtime_actual or event.realtime_forecast
    suffix = "" if real is None else f" ({'act' if event.realtime_actual else 'exp'} {_hhmm(real)})"
    return f"{_hhmm(event.schedule_advertised)}{suffix}"


def _hhmm(when: datetime | None) -> str:
    return f"{when:%H:%M}" if when else "--:--"


if __name__ == "__main__":
    main()

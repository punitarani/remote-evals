"""Capture and validate a small, replayable nonstop flight inventory."""

import argparse
import json
import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

from fli.models import (
    Airport,
    DateSearchFilters,
    FlightSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SortBy,
)
from fli.search import SearchDates, SearchFlights
from fli.search.exceptions import (
    SearchConnectionError,
    SearchHTTPError,
    SearchTimeoutError,
)
from fli.search.flights import SearchParseError

LOCALE = {"currency": "USD", "country": "US"}


def price_cents(price, currency):
    cents = Decimal(str(price)) * 100
    if currency != "USD" or not cents.is_finite() or cents <= 0 or cents % 1:
        raise ValueError(
            f"Expected a positive USD fare in whole cents, got {price} {currency}"
        )
    return int(cents)


def normalize_flight(result, origin, destination, day):
    """Keep replay fields only, including the full timestamps Fli returns."""
    if len(result.legs) != 1:
        raise ValueError("Expected a nonstop itinerary")
    leg = result.legs[0]
    if (
        leg.departure_airport != origin
        or leg.arrival_airport != destination
        or leg.departure_datetime.date() != day
    ):
        raise ValueError("Flight does not match the requested route and departure date")
    return {
        "date": day.isoformat(),
        "price_cents": price_cents(result.price, result.currency),
        "airline": leg.airline.name,
        "flight_number": leg.flight_number,
        "departure": leg.departure_datetime.isoformat(),
        "arrival": leg.arrival_datetime.isoformat(),
        "minutes": leg.duration,
    }


def search_day(search, filters, day):
    # Fli can return an empty list without an error. Retry once before
    # treating the date as a gap in coverage.
    for attempt in range(2):
        try:
            rows = search(filters, **LOCALE)
        except (SearchConnectionError, SearchTimeoutError, SearchHTTPError) as exc:
            if isinstance(exc, SearchHTTPError) and exc.status_code not in (
                429,
                500,
                502,
                503,
                504,
            ):
                raise ValueError(f"{day}: flight search failed: {exc}") from exc
            if attempt:
                raise ValueError(f"{day}: flight search failed twice: {exc}") from exc
        else:
            if rows:
                return rows
    raise ValueError(f"{day}: Fli returned no nonstop offers after two attempts")


def capture(origin, destination, start, days, as_of):
    if origin == destination:
        raise ValueError("Origin and destination must differ")
    if not 2 <= days <= 7 or start <= as_of.date():
        raise ValueError("Choose two to seven dates starting after today in UTC")

    def segment(day):
        return FlightSegment(
            departure_airport=[[origin, 0]],
            arrival_airport=[[destination, 0]],
            travel_date=day.isoformat(),
        )

    flights = SearchFlights()
    passengers = PassengerInfo(adults=1)
    end = start + timedelta(days=days - 1)
    offers = []
    counts = {}
    minima = {}
    for offset in range(days):
        day = start + timedelta(days=offset)
        filters = FlightSearchFilters(
            passenger_info=passengers,
            flight_segments=[segment(day)],
            stops=MaxStops.NON_STOP,
            sort_by=SortBy.BEST,
            show_all_results=True,
        )
        try:
            rows = [
                normalize_flight(result, origin, destination, day)
                for result in search_day(flights.search, filters, day)
            ]
        except (ValueError, AttributeError, SearchParseError) as exc:
            raise ValueError(f"Could not capture offers for {day}: {exc}") from exc
        counts[day.isoformat()] = len(rows)
        minima[day.isoformat()] = min(row["price_cents"] for row in rows)
        offers.extend(rows)

    # Check the flight results against the separate date search before saving.
    calendar = SearchDates().search(
        DateSearchFilters(
            passenger_info=passengers,
            flight_segments=[segment(start)],
            stops=MaxStops.NON_STOP,
            from_date=start.isoformat(),
            to_date=end.isoformat(),
        ),
        **LOCALE,
    )
    quoted = {}
    for row in calendar or []:
        day = row.date[0].date().isoformat()
        if day in quoted or day not in minima:
            raise ValueError(f"{day}: unexpected or duplicate date in Fli calendar")
        quoted[day] = price_cents(row.price, row.currency)
    for day, minimum in minima.items():
        if quoted.get(day) != minimum:
            raise ValueError(
                f"{day}: calendar minimum {quoted.get(day)} differs from captured minimum "
                f"{minimum} cents"
            )

    offers.sort(
        key=lambda row: (
            row["date"],
            row["price_cents"],
            row["airline"],
            row["flight_number"],
            row["departure"],
            row["arrival"],
            row["minutes"],
        )
    )
    return {
        "as_of": as_of.isoformat(),
        "origin": origin.name,
        "destination": destination.name,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "source": {
            "flights_version": version("flights"),
            "country": LOCALE["country"],
            "currency": LOCALE["currency"],
            "cabin_class": "ECONOMY",
            "passengers": 1,
            "trip_type": "ONE_WAY",
            "max_stops": "NON_STOP",
            "sort_by": "BEST",
            "show_all_results": True,
            "offers_per_date": counts,
        },
        "itineraries": offers,
    }


def write_snapshot(path, snapshot):
    """Replace the snapshot atomically and return the digest of the saved bytes."""
    data = (json.dumps(snapshot, indent=2, allow_nan=False) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temp = Path(output.name)
        try:
            with output:
                output.write(data)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    return sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    as_of = datetime.now(UTC).replace(microsecond=0)
    try:
        snapshot = capture(
            Airport[args.origin.upper()],
            Airport[args.destination.upper()],
            args.start_date or as_of.date() + timedelta(days=21),
            args.days,
            as_of,
        )
        digest = write_snapshot(args.output, snapshot)
    except Exception as exc:
        parser.exit(1, f"Capture failed: {exc}\n")
    print(f"{args.output} sha256:{digest}")
    for day, count in snapshot["source"]["offers_per_date"].items():
        print(f"  {day}: {count} nonstop offers")


if __name__ == "__main__":
    main()

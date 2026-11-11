"""Serve the pinned Fli MCP tools against one captured nonstop inventory."""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import time_machine
from fastmcp import FastMCP
from fli.models import (
    Airline,
    Airport,
    FlightLeg,
    FlightResult,
    MaxStops,
    SeatType,
    SortBy,
    TripType,
)
from fli.search.dates import DatePrice


class Inventory:
    def __init__(self, data):
        self.data = data

    def matching(self, filters, currency, country, start_date, end_date):
        segment = filters.flight_segments[0]
        if (
            filters.trip_type != TripType.ONE_WAY
            or filters.passenger_info.adults != 1
            or filters.seat_type != SeatType.ECONOMY
            or filters.stops != MaxStops.NON_STOP
            or currency not in (None, "USD")
            or country not in (None, "US")
        ):
            raise ValueError(
                "Snapshot covers one-way economy USD nonstops for one adult in the US locale"
            )
        if any(
            (
                filters.airlines,
                filters.airlines_exclude,
                filters.alliances,
                filters.alliances_exclude,
                filters.layover_restrictions,
                filters.bags,
                segment.time_restrictions,
            )
        ):
            raise ValueError("Snapshot does not support extra flight filters")
        if segment.departure_airport != [
            [Airport[self.data["origin"]], 0]
        ] or segment.arrival_airport != [[Airport[self.data["destination"]], 0]]:
            raise ValueError(
                f"Snapshot only covers {self.data['origin']} to {self.data['destination']}"
            )
        if (
            not self.data["start_date"]
            <= start_date
            <= end_date
            <= self.data["end_date"]
        ):
            raise ValueError(
                f"Snapshot only covers {self.data['start_date']} through {self.data['end_date']}"
            )
        return [
            row
            for row in self.data["itineraries"]
            if start_date <= row["date"] <= end_date
        ]

    def search_dates(self, filters, currency=None, country=None, **_locale):
        prices = {}
        for row in self.matching(
            filters, currency, country, filters.from_date, filters.to_date
        ):
            day, price = row["date"], row["price_cents"]
            prices[day] = min(price, prices.get(day, price))
        return [
            DatePrice(
                date=(datetime.fromisoformat(day),), price=price / 100, currency="USD"
            )
            for day, price in sorted(prices.items())
        ]

    def search_flights(self, filters, currency=None, country=None, **_locale):
        if filters.sort_by not in (SortBy.CHEAPEST, SortBy.BEST):
            raise ValueError("Snapshot supports BEST and CHEAPEST flight sorting")
        if filters.exclude_basic_economy or filters.emissions.name != "ALL":
            raise ValueError("Snapshot does not support extra flight filters")
        day = filters.flight_segments[0].travel_date
        return [
            FlightResult(
                legs=[
                    FlightLeg(
                        airline=Airline[row["airline"]],
                        flight_number=row["flight_number"],
                        departure_airport=Airport[self.data["origin"]],
                        arrival_airport=Airport[self.data["destination"]],
                        departure_datetime=datetime.fromisoformat(row["departure"]),
                        arrival_datetime=datetime.fromisoformat(row["arrival"]),
                        duration=row["minutes"],
                    )
                ],
                price=row["price_cents"] / 100,
                currency="USD",
                duration=row["minutes"],
                stops=0,
            )
            for row in self.matching(filters, currency, country, day, day)
        ]


async def main():
    # Keep Fli's MCP defaults and calendar independent of the host process.
    for name in tuple(os.environ):
        if name.upper().startswith("FLI_MCP_"):
            del os.environ[name]
    os.environ["TZ"] = "UTC"
    time.tzset()
    from fli.mcp import server as fli

    data = json.loads(
        Path(__file__).with_name("fixture.json").read_text(encoding="utf-8")
    )
    inventory = Inventory(data)
    server = FastMCP("Emulated Fli")
    for name in ("search_dates", "search_flights"):
        tool = await fli.mcp.get_tool(name)
        if tool is None:
            raise RuntimeError(f"Pinned Fli version does not provide {name}")
        server.add_tool(tool)

    # Keep Fli's real MCP validation and serialization while replacing network searches.
    with (
        time_machine.travel(datetime.fromisoformat(data["as_of"]), tick=False),
        patch.object(
            fli,
            "SearchDates",
            return_value=SimpleNamespace(search=inventory.search_dates),
        ),
        patch.object(
            fli,
            "SearchFlights",
            return_value=SimpleNamespace(search=inventory.search_flights),
        ),
    ):
        await server.run_async(transport="stdio", show_banner=False)


if __name__ == "__main__":
    asyncio.run(main())

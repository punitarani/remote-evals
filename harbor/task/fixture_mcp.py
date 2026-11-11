"""Serve the two Fli tools with an inventory that lives entirely in the trial."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import time_machine
from fastmcp import FastMCP
from fli.mcp import server as fli
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

    def matching(self, filters, currency, start_date, end_date):
        if filters.trip_type != TripType.ONE_WAY or filters.passenger_info.adults != 1:
            raise ValueError("Fixture supports one-way travel for one adult")
        if currency not in (None, "USD"):
            raise ValueError("Fixture prices are in USD")

        segment = filters.flight_segments[0]
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
            raise ValueError("Fixture does not support extra flight filters")

        origin = Airport[self.data["origin"]]
        destination = Airport[self.data["destination"]]
        if (
            [origin, 0] not in segment.departure_airport
            or [destination, 0] not in segment.arrival_airport
            or filters.seat_type != SeatType.ECONOMY
        ):
            return []

        return [
            item
            for item in self.data["itineraries"]
            if start_date <= item["date"] <= end_date
            and (filters.stops != MaxStops.NON_STOP or len(item["legs"]) == 1)
        ]

    def search_dates(self, filters, currency=None, **_locale):
        prices = {}
        for item in self.matching(
            filters, currency, filters.from_date, filters.to_date
        ):
            date, price = item["date"], item["price_cents"] / 100
            prices[date] = min(price, prices.get(date, price))
        return [
            DatePrice(date=(datetime.fromisoformat(date),), price=price, currency="USD")
            for date, price in sorted(prices.items())
        ]

    def search_flights(self, filters, currency=None, **_locale):
        if filters.sort_by not in (SortBy.CHEAPEST, SortBy.BEST):
            raise ValueError("Fixture supports cheapest-flight sorting")
        if filters.exclude_basic_economy or filters.emissions.name != "ALL":
            raise ValueError("Fixture does not support extra flight filters")
        date = filters.flight_segments[0].travel_date
        items = self.matching(filters, currency, date, date)
        return [
            flight_result(item)
            for item in sorted(items, key=lambda item: item["price_cents"])
        ]


def flight_result(item):
    date = item["date"]
    legs = [
        FlightLeg(
            airline=Airline[leg["flight"][:2]],
            flight_number=leg["flight"][2:],
            departure_airport=Airport[leg["from"]],
            arrival_airport=Airport[leg["to"]],
            departure_datetime=datetime.fromisoformat(f"{date}T{leg['departure']}"),
            arrival_datetime=datetime.fromisoformat(f"{date}T{leg['arrival']}"),
            duration=leg["minutes"],
        )
        for leg in item["legs"]
    ]
    return FlightResult(
        legs=legs,
        price=item["price_cents"] / 100,
        currency="USD",
        duration=sum(leg.duration for leg in legs),
        stops=len(legs) - 1,
    )


async def main():
    data = json.loads(
        Path(__file__).with_name("fixture.json").read_text(encoding="utf-8")
    )
    inventory = Inventory(data)
    server = FastMCP("Fixture-backed Fli")
    for name in ("search_dates", "search_flights"):
        tool = await fli.mcp.get_tool(name)
        if tool is None:
            raise RuntimeError(f"Pinned Fli version does not provide {name}")
        server.add_tool(tool)

    # Only this process replaces Fli's search clients and clock. Its MCP tools
    # still run Fli's input validation, filter construction, and serialization.
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

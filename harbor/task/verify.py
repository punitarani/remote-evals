"""Grade the transferred report using the fixture bundled with the verifier."""

import argparse
import json
import re
from datetime import date
from pathlib import Path

MAX_REPORT_BYTES = 16_384


def expected_report(fixture, budget_cents=None):
    origin, destination = fixture["origin"], fixture["destination"]
    price_cents, date, flight_number = min(
        (
            row["price_cents"],
            row["date"],
            row["airline"].lstrip("_") + row["flight_number"],
        )
        for row in fixture["itineraries"]
    )
    if budget_cents is not None and price_cents > budget_cents:
        return {"status": "no_match"}
    return {
        "status": "found",
        "origin": origin,
        "destination": destination,
        "departure_date": date,
        "flight_number": flight_number,
        "currency": "USD",
        "price_cents": price_cents,
    }


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def grade(path, expected, *, live_scope=None):
    if path.is_symlink() or not path.is_file():
        return 0, "Missing regular report.json file"
    with path.open("rb") as report_file:
        raw = report_file.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        return 0, "report.json exceeds 16 KiB"
    try:
        report = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys)
    except ValueError, RecursionError:
        return 0, "report.json is not a valid JSON document with unique keys"
    if not isinstance(report, dict) or report.keys() != expected.keys():
        return 0, f"Expected a JSON object with fields: {', '.join(expected)}"
    if live_scope is not None:
        if any(type(report[key]) is not type(value) for key, value in expected.items()):
            return 0, "Live report fields have incorrect types"
        if any(
            report[key] != expected[key]
            for key in ("status", "origin", "destination", "currency")
        ):
            return (
                0,
                "Live report does not describe a found USD itinerary on the requested route",
            )
        try:
            departure = date.fromisoformat(report["departure_date"]).isoformat()
        except ValueError:
            return 0, "Live report has an invalid departure date"
        if not (
            departure == report["departure_date"]
            and live_scope["start_date"] <= departure <= live_scope["end_date"]
            and re.fullmatch(r"[A-Z0-9]{2}[0-9]+[A-Z]?", report["flight_number"])
            and report["price_cents"] > 0
        ):
            return 0, "Live report has an invalid date, flight number, or fare"
        return 1, "Live report matches the itinerary contract"
    for key, value in expected.items():
        if type(report[key]) is not type(value) or report[key] != value:
            return 0, f"Incorrect {key}. Expected {value!r}"
    return 1, "Report matches the fixture"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("logs", type=Path)
    args = parser.parse_args()
    fixture = json.loads(
        Path(__file__).with_name("fixture.json").read_text(encoding="utf-8")
    )
    if fixture.get("mode") == "live":
        shape = {
            "status": "found",
            "origin": fixture["origin"],
            "destination": fixture["destination"],
            "departure_date": "",
            "flight_number": "",
            "currency": "USD",
            "price_cents": 0,
        }
        reward, diagnostic = grade(args.report, shape, live_scope=fixture)
    else:
        budget_path = Path(__file__).with_name("budget-cents.txt")
        budget_cents = int(budget_path.read_text()) if budget_path.exists() else None
        reward, diagnostic = grade(args.report, expected_report(fixture, budget_cents))
    args.logs.mkdir(parents=True, exist_ok=True)
    (args.logs / "reward.txt").write_text(f"{reward}\n", encoding="utf-8")
    (args.logs / "diagnostic.txt").write_text(f"{diagnostic}\n", encoding="utf-8")


if __name__ == "__main__":
    main()

"""Grade the transferred report using the fixture bundled with the verifier."""

import argparse
import json
from pathlib import Path

MAX_REPORT_BYTES = 16_384


def expected_report(fixture):
    origin, destination = fixture["origin"], fixture["destination"]
    options = [
        (item["price_cents"], item["date"], item["legs"][0]["flight"])
        for item in fixture["itineraries"]
        if len(item["legs"]) == 1
        and item["legs"][0]["from"] == origin
        and item["legs"][0]["to"] == destination
        and fixture["start_date"] <= item["date"] <= fixture["end_date"]
    ]
    if not options:
        return {"status": "no_match"}

    price_cents, date, flight_number = min(options)
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


def grade(path, expected):
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
    reward, diagnostic = grade(args.report, expected_report(fixture))
    args.logs.mkdir(parents=True, exist_ok=True)
    (args.logs / "reward.txt").write_text(f"{reward}\n", encoding="utf-8")
    (args.logs / "diagnostic.txt").write_text(f"{diagnostic}\n", encoding="utf-8")


if __name__ == "__main__":
    main()

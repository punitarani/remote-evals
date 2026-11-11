import importlib
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fli.models import Airline, Airport
from fli.search.exceptions import SearchTimeoutError
from test_task import TASK_SOURCE


@pytest.fixture
def capture_module(monkeypatch):
    monkeypatch.syspath_prepend(str(TASK_SOURCE))
    return importlib.import_module("capture_fli")


def test_capture_retains_every_offer_and_overnight_arrival(capture_module, monkeypatch):
    day = date(2026, 12, 2)
    seen = []

    def search(filters, **locale):
        assert locale == {"currency": "USD", "country": "US"}
        assert filters.show_all_results and filters.stops.name == "NON_STOP"
        departure = datetime.fromisoformat(
            filters.flight_segments[0].travel_date + "T23:30"
        )
        seen.append(departure.date())
        if len(seen) == 1:
            raise SearchTimeoutError("temporary")
        return [
            SimpleNamespace(
                price=price,
                currency="USD",
                legs=[
                    SimpleNamespace(
                        departure_airport=Airport.SFO,
                        arrival_airport=Airport.JFK,
                        airline=Airline.AA,
                        flight_number=number,
                        duration=325,
                        departure_datetime=departure,
                        arrival_datetime=departure + timedelta(hours=8),
                    )
                ],
            )
            for price, number in [(124.00, "200"), (105.25, "166")]
        ]

    quotes = [
        SimpleNamespace(
            date=(datetime(2026, 12, offset),), price=105.25, currency="USD"
        )
        for offset in (2, 3)
    ]
    monkeypatch.setattr(
        capture_module, "SearchFlights", lambda: SimpleNamespace(search=search)
    )
    monkeypatch.setattr(
        capture_module,
        "SearchDates",
        lambda: SimpleNamespace(search=lambda *_a, **_k: quotes),
    )
    as_of = datetime(2026, 11, 11, tzinfo=UTC)
    snapshot = capture_module.capture(Airport.SFO, Airport.JFK, day, 2, as_of)
    assert seen == [day, day, date(2026, 12, 3)]
    assert snapshot["source"]["offers_per_date"] == {"2026-12-02": 2, "2026-12-03": 2}
    assert snapshot["itineraries"][0]["price_cents"] == 10525
    assert snapshot["itineraries"][0]["arrival"] == "2026-12-03T07:30:00"
    quotes.pop()
    with pytest.raises(ValueError, match="2026-12-03.*calendar minimum"):
        capture_module.capture(Airport.SFO, Airport.JFK, day, 2, as_of)


def test_empty_day_does_not_look_like_coverage(capture_module):
    attempts = []

    def search(filters, **locale):
        attempts.append(locale)
        return []

    with pytest.raises(ValueError, match="2026-12-02.*no nonstop offers"):
        capture_module.search_day(search, None, date(2026, 12, 2))
    assert len(attempts) == 2


def test_failed_replacement_preserves_output(capture_module, tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"
    path.write_text("old")

    def fail(*_args):
        raise OSError("write failed")

    monkeypatch.setattr(type(path), "replace", fail)
    with pytest.raises(OSError, match="write failed"):
        capture_module.write_snapshot(path, {"new": True})
    assert path.read_text() == "old"
    assert list(tmp_path.iterdir()) == [path]

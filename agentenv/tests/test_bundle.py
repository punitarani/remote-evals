import asyncio
import json
import sys

from agentenv.__main__ import build_bundle
from agentenv.fli_client import call_tool


def test_showcase_bundle_keeps_verifier_inputs_off_the_container(tmp_path):
    bundle = build_bundle(tmp_path, "showcase")
    tasks = tmp_path / "tasks"

    assert {path.stem for path in (bundle / "tasks").glob("*.json")} == {
        "cheapest-nonstop",
        "under-budget",
        "live-smoke",
    }
    for definition in (bundle / "tasks").glob("*.json"):
        sandbox, flight, report = json.loads(definition.read_text())
        assert [step["type"] for step in (sandbox, flight, report)] == [
            "deploy_sandbox",
            "run_flight_task",
            "grade_flight_report",
        ]
        assert sandbox["sandbox_type"] == "daytona"
        assert flight["depends_on"] == [sandbox["id"]]
        assert report["depends_on"] == [flight["id"]]
        assert flight["task_name"] == report["task_name"] == definition.stem
    budget = tasks / "under-budget"
    assert (budget / "tests" / "budget-cents.txt").read_text() == "15000\n"
    assert not (budget / "environment" / "payload" / "budget-cents.txt").exists()
    assert not (
        tasks / "live-smoke" / "environment" / "payload" / "fixture.json"
    ).exists()


def test_trial_mcp_client_reads_structured_results_from_the_locked_fixture(tmp_path):
    build_bundle(tmp_path, "smoke")
    payload = tmp_path / "tasks" / "cheapest-nonstop" / "environment" / "payload"
    fixture = json.loads((payload / "fixture.json").read_text())
    result = asyncio.run(
        call_tool(
            "search_dates",
            json.dumps(
                {
                    "origin": fixture["origin"],
                    "destination": fixture["destination"],
                    "start_date": fixture["start_date"],
                    "end_date": fixture["end_date"],
                    "passengers": 1,
                    "cabin_class": "ECONOMY",
                    "max_stops": "NON_STOP",
                    "currency": "USD",
                    "country": "US",
                }
            ),
            sys.executable,
            str(payload / "fixture_mcp.py"),
        )
    )
    assert result["success"]
    assert len(result["dates"]) == 3

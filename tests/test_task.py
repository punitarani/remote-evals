import asyncio
import json
import subprocess
import sys
import tomllib
from hashlib import sha256
from pathlib import Path

import pytest
from harbor.models.task.task import Task
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
TASK_SOURCE = ROOT / "harbor" / "task"


@pytest.fixture(scope="module")
def tasks(tmp_path_factory):
    destination = tmp_path_factory.mktemp("tasks")
    for suite in ("smoke", "live-smoke"):
        subprocess.run(
            [
                sys.executable,
                TASK_SOURCE / "package.py",
                destination / suite,
                "--suite",
                suite,
            ],
            check=True,
        )
    return destination


def test_packaged_tasks_select_the_expected_mcp_and_network(tasks):
    for suite, task_name in (
        ("smoke", "cheapest-nonstop"),
        ("live-smoke", "live-smoke"),
    ):
        task = tasks / suite / task_name
        assert Task.is_valid_dir(task)
        config = tomllib.loads((task / "task.toml").read_text())
        assert config["artifacts"] == [
            {"source": "/workspace/report.json", "destination": "report.json"}
        ]
        assert config["environment"]["workdir"] == "/workspace"
        assert config["verifier"]["environment_mode"] == "separate"
        assert config["verifier"]["environment"]["network_mode"] == "no-network"
        mcp = config["environment"]["mcp_servers"][0]
        assert mcp["name"] == "fli"
        payload = task / "environment" / "payload"
        fixture = (task / "tests" / "fixture.json").read_bytes()
        if suite == "smoke":
            assert config["agent"]["network_mode"] == "no-network"
            assert mcp["args"] == ["/opt/fixture/fixture_mcp.py"]
            assert (payload / "fixture.json").read_bytes() == fixture
            assert config["metadata"]["fixture_sha256"] == sha256(fixture).hexdigest()
        else:
            assert config["agent"]["network_mode"] == "allowlist"
            assert config["agent"]["allowed_hosts"] == ["www.google.com"]
            assert mcp["command"].endswith("/fli-mcp")
            assert not (payload / "fixture.json").exists()
            assert "itineraries" not in json.loads(fixture)


FOUND = {
    "status": "found",
    "origin": "SFO",
    "destination": "JFK",
    "departure_date": "2026-12-03",
    "flight_number": "AA166",
    "currency": "USD",
    "price_cents": 17400,
}


@pytest.mark.parametrize(
    ("changes", "reward"),
    [
        ({}, "1"),
        (None, "0"),
        ({"flight_number": "UA208"}, "0"),
        ({"price_cents": 17400.0}, "0"),
    ],
)
def test_emulated_verifier_grades_the_same_report_identically(
    tasks, tmp_path, changes, reward
):
    report = None if changes is None else {**FOUND, **changes}
    verify(tasks / "smoke" / "cheapest-nonstop", tmp_path, report, reward)
    verify(tasks / "smoke" / "cheapest-nonstop", tmp_path, report, reward)


def test_live_verifier_checks_contract_without_a_snapshot_price(tasks, tmp_path):
    task = tasks / "live-smoke" / "live-smoke"
    scope = json.loads((task / "tests" / "fixture.json").read_text())
    report = {**FOUND, "departure_date": scope["start_date"], "price_cents": 27501}
    verify(task, tmp_path, report, "1")
    verify(task, tmp_path, {**report, "departure_date": "2030-02-31"}, "0")
    verify(task, tmp_path, {"status": "no_match"}, "0")


def verify(task, tmp_path, report, reward):
    report_file = tmp_path / "report.json"
    if report is not None:
        report_file.write_text(json.dumps(report))
    subprocess.run(
        [sys.executable, task / "tests" / "verify.py", report_file, tmp_path],
        check=True,
    )
    assert (tmp_path / "reward.txt").read_text().strip() == reward
    assert (tmp_path / "diagnostic.txt").read_text().strip()


async def query_mcp(command, args, calls):
    params = StdioServerParameters(command=command, args=args)
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        await client.initialize()
        tools = {
            tool.name: tool.model_dump(by_alias=True)["inputSchema"]
            for tool in (await client.list_tools()).tools
        }
        results = [await client.call_tool(name, arguments) for name, arguments in calls]
    return tools, results


def test_fli_snapshot_replays_identically_across_fresh_processes(tasks):
    payload = tasks / "smoke" / "cheapest-nonstop" / "environment" / "payload"
    snapshot = json.loads((payload / "fixture.json").read_text())
    route = {
        "origin": "SFO",
        "destination": "JFK",
        "currency": "USD",
        "max_stops": "NON_STOP",
    }
    dates = {
        **route,
        "start_date": snapshot["start_date"],
        "end_date": snapshot["end_date"],
    }
    flight_calls = [
        ("search_flights", {**route, "departure_date": day})
        for day in snapshot["source"]["offers_per_date"]
    ]
    calls = [
        ("search_dates", dates),
        *flight_calls,
        ("search_flights", {**route, "departure_date": "2029-12-01"}),
        ("search_dates", {**dates, "destination": "LAX"}),
        ("search_dates", {**dates, "max_stops": "ANY"}),
    ]
    command = [str(payload / "fixture_mcp.py")]
    fixture_tools, results = asyncio.run(query_mcp(sys.executable, command, calls))
    _, repeated = asyncio.run(query_mcp(sys.executable, command, calls))
    assert results == repeated
    live_tools, _ = asyncio.run(
        query_mcp(sys.executable, ["-c", "from fli.mcp import run; run()"], [])
    )
    assert fixture_tools == {
        name: live_tools[name] for name in ("search_dates", "search_flights")
    }
    data = [result.model_dump(by_alias=True)["structuredContent"] for result in results]
    offers_by_date = data[1 : 1 + len(flight_calls)]
    counts = list(snapshot["source"]["offers_per_date"].values())
    assert [len(day["flights"]) for day in offers_by_date] == counts
    assert [day["price"] for day in data[0]["dates"]] == [
        min(flight["price"] for flight in day["flights"]) for day in offers_by_date
    ]
    assert all(
        not item["success"] and "Snapshot" in item["error"] for item in data[-3:]
    )

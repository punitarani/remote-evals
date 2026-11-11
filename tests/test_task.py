import asyncio
import json
import subprocess
import sys
import tomllib
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
    subprocess.run(
        [sys.executable, TASK_SOURCE / "package.py", destination, "--suite", "full"],
        check=True,
    )
    return destination


def test_packaged_tasks_configure_workspace_artifacts_and_separate_verifier(tasks):
    assert sorted(path.name for path in tasks.iterdir()) == [
        "cheapest-nonstop",
        "no-match",
    ]
    for task in tasks.iterdir():
        assert Task.is_valid_dir(task)
        config = tomllib.loads((task / "task.toml").read_text())
        assert config["artifacts"] == [
            {"source": "/workspace/report.json", "destination": "report.json"}
        ]
        assert config["environment"]["workdir"] == "/workspace"
        assert config["verifier"]["environment_mode"] == "separate"
        assert config["verifier"]["environment"]["network_mode"] == "no-network"
        assert config["environment"]["mcp_servers"][0]["name"] == "fli"
        assert (task / "environment" / "fixture.json").read_bytes() == (
            task / "tests" / "fixture.json"
        ).read_bytes()


FOUND = {
    "status": "found",
    "origin": "SFO",
    "destination": "JFK",
    "departure_date": "2030-05-10",
    "flight_number": "UA202",
    "currency": "USD",
    "price_cents": 19900,
}


@pytest.mark.parametrize(
    ("task_name", "report", "reward"),
    [
        ("cheapest-nonstop", json.dumps(FOUND), "1"),
        ("cheapest-nonstop", None, "0"),
        ("cheapest-nonstop", '{"status":', "0"),
        ("cheapest-nonstop", json.dumps({**FOUND, "flight_number": "UA208"}), "0"),
        ("cheapest-nonstop", json.dumps({**FOUND, "price_cents": 19900.0}), "0"),
        ("no-match", json.dumps(FOUND), "0"),
        ("no-match", '{"status":"no_match"}', "1"),
    ],
)
def test_verifier_grades_the_report(tasks, tmp_path, task_name, report, reward):
    report_file = tmp_path / "report.json"
    if report is not None:
        report_file.write_text(report)
    subprocess.run(
        [
            sys.executable,
            tasks / task_name / "tests" / "verify.py",
            report_file,
            tmp_path,
        ],
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


@pytest.mark.parametrize("task_name", ["cheapest-nonstop", "no-match"])
def test_fixture_uses_live_fli_schemas_and_honors_nonstop_filter(tasks, task_name):
    route = {"origin": "SFO", "destination": "JFK", "currency": "USD"}
    date_range = {**route, "start_date": "2030-05-10", "end_date": "2030-05-12"}
    departure = {**route, "departure_date": "2030-05-10"}
    calls = [
        ("search_dates", date_range),
        ("search_dates", {**date_range, "max_stops": "NON_STOP"}),
        ("search_flights", {**departure, "max_stops": "NON_STOP"}),
    ]
    fixture_tools, results = asyncio.run(
        query_mcp(
            sys.executable,
            [str(tasks / task_name / "environment" / "fixture_mcp.py")],
            calls,
        )
    )
    live_tools, _ = asyncio.run(
        query_mcp(sys.executable, ["-c", "from fli.mcp import run; run()"], [])
    )
    assert fixture_tools == {
        name: live_tools[name] for name in ("search_dates", "search_flights")
    }
    wire_results = [result.model_dump(by_alias=True) for result in results]
    assert all(not result["isError"] for result in wire_results)
    any_stops, nonstop, flights = [
        result["structuredContent"] for result in wire_results
    ]
    assert all(result["success"] for result in (any_stops, nonstop, flights))
    assert any_stops["dates"][0]["price"] == 129.0
    if task_name == "cheapest-nonstop":
        assert [entry["price"] for entry in nonstop["dates"]] == [199.0, 199.0, 249.0]
        assert [item["legs"][0]["flight_number"] for item in flights["flights"]] == [
            "208",
            "202",
        ]
        assert all(len(item["legs"]) == 1 for item in flights["flights"])
    else:
        assert nonstop["dates"] == []
        assert flights["flights"] == []

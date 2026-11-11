"""Materialize the emulated flight task or its live Harbor smoke test."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from shutil import copyfile

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parents[1]
SUITES = {"smoke": "cheapest-nonstop", "live-smoke": "live-smoke"}
SNAPSHOT = SOURCE / "fixtures" / "sfo-jfk.json"


def prepare_tasks(destination: Path, suite: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError(f"Task destination must be empty: {destination}")

    name = SUITES[suite]
    task = destination / name
    environment = task / "environment"
    payload = environment / "payload"
    verifier = task / "tests"
    payload.mkdir(parents=True)
    verifier.mkdir()

    snapshot = SNAPSHOT.read_bytes()
    fixture = json.loads(snapshot)
    scope = {
        key: fixture[key] for key in ("origin", "destination", "start_date", "end_date")
    }
    if suite == "live-smoke":
        start = datetime.now(UTC).date() + timedelta(days=21)
        scope.update(
            start_date=start.isoformat(),
            end_date=(start + timedelta(days=2)).isoformat(),
        )
        verifier_data = json.dumps({**scope, "mode": "live"}).encode()
        metadata = 'fli_mode = "live"'
        network = 'network_mode = "allowlist"\nallowed_hosts = ["www.google.com"]'
        mcp = 'command = "/opt/fixture/.venv/bin/fli-mcp"\nargs = []'
    else:
        verifier_data = snapshot
        metadata = (
            f'fli_mode = "emulated"\nfixture_sha256 = "{sha256(snapshot).hexdigest()}"'
        )
        network = 'network_mode = "no-network"'
        mcp = 'command = "/opt/fixture/.venv/bin/python"\nargs = ["/opt/fixture/fixture_mcp.py"]'
        (payload / "fixture.json").write_bytes(snapshot)
    (verifier / "fixture.json").write_bytes(verifier_data)

    manifest = (SOURCE / "task.toml.in").read_text(encoding="utf-8")
    (task / "task.toml").write_text(
        manifest.replace("TASK_NAME", name)
        .replace("TASK_METADATA", metadata)
        .replace("AGENT_NETWORK", network)
        .replace("MCP_COMMAND", mcp),
        encoding="utf-8",
    )
    instruction = (SOURCE / "instruction.md").read_text(encoding="utf-8")
    for key, value in scope.items():
        instruction = instruction.replace("{{" + key + "}}", value)
    (task / "instruction.md").write_text(instruction, encoding="utf-8")

    for file in ("Dockerfile", "harbor-constraints.txt"):
        copyfile(SOURCE / file, environment / file)
    copyfile(SOURCE / "fixture_mcp.py", payload / "fixture_mcp.py")
    for file in ("pyproject.toml", "uv.lock"):
        copyfile(ROOT / file, environment / file)
    for file in ("verify.py", "test.sh"):
        copyfile(SOURCE / file, verifier / file)
    (verifier / "test.sh").chmod(0o755)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--suite", choices=SUITES, default="smoke")
    args = parser.parse_args()
    prepare_tasks(args.destination, args.suite)


if __name__ == "__main__":
    main()

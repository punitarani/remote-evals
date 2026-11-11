"""Materialize standalone Harbor tasks from the shared task source."""

import argparse
from hashlib import sha256
from pathlib import Path
from shutil import copyfile

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parents[1]
SUITES = {"smoke": ("cheapest-nonstop",), "full": ("cheapest-nonstop", "no-match")}


def prepare_tasks(destination: Path, suite: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError(f"Task destination must be empty: {destination}")

    for name in SUITES[suite]:
        task = destination / name
        environment = task / "environment"
        verifier = task / "tests"
        environment.mkdir(parents=True)
        verifier.mkdir()

        fixture = SOURCE / "fixtures" / f"{name}.json"
        fixture_digest = sha256(fixture.read_bytes()).hexdigest()
        manifest = (SOURCE / "task.toml.in").read_text(encoding="utf-8")
        (task / "task.toml").write_text(
            manifest.replace("TASK_NAME", name).replace(
                "FIXTURE_SHA256", fixture_digest
            ),
            encoding="utf-8",
        )
        copyfile(SOURCE / "instruction.md", task / "instruction.md")
        copyfile(SOURCE / "Dockerfile", environment / "Dockerfile")
        copyfile(
            SOURCE / "harbor-constraints.txt", environment / "harbor-constraints.txt"
        )
        copyfile(SOURCE / "fixture_mcp.py", environment / "fixture_mcp.py")
        copyfile(ROOT / "pyproject.toml", environment / "pyproject.toml")
        copyfile(ROOT / "uv.lock", environment / "uv.lock")
        copyfile(fixture, environment / "fixture.json")
        copyfile(fixture, verifier / "fixture.json")
        copyfile(SOURCE / "verify.py", verifier / "verify.py")
        copyfile(SOURCE / "test.sh", verifier / "test.sh")
        (verifier / "test.sh").chmod(0o755)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--suite", choices=SUITES, default="smoke")
    args = parser.parse_args()
    prepare_tasks(args.destination, args.suite)


if __name__ == "__main__":
    main()

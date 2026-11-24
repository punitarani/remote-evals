"""Run flight evals in AgentEnv with a remote sandbox and local grading."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path
from string import Template
from uuid import uuid4

PROJECT = Path(__file__).resolve().parent
ROOT = PROJECT.parent
SUITES = ("smoke", "budget", "live-smoke", "showcase")
DEFAULT_SANDBOX = "daytona"
SANDBOXES = {
    "daytona": ("DAYTONA_API_KEY", "@local/remote-evals/flights"),
    "sail_vm": ("SAIL_API_KEY", "@local/remote-evals/flights-sail"),
}


def build_bundle(job: Path, suite: str, *, sandbox: str = DEFAULT_SANDBOX) -> Path:
    tasks = job / "tasks"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "harbor/task/package.py"),
            str(tasks),
            "--suite",
            suite,
        ],
        check=True,
    )
    bundle = job / "bundle"
    (bundle / "tasks").mkdir(parents=True)
    template = Template((PROJECT / "tasks/flight.json.in").read_text(encoding="utf-8"))
    for task in sorted(tasks.iterdir()):
        (bundle / "tasks" / f"{task.name}.json").write_text(
            template.substitute(task_name=task.name, sandbox=sandbox),
            encoding="utf-8",
        )
    return bundle


def main() -> int:
    from agent_env.bundle.parse import BundleError
    from agent_env.bundle.run import Outcome, RunInterrupted, dry_run_bundle, run_bundle

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=SUITES, default="smoke")
    parser.add_argument(
        "--sandbox",
        choices=SANDBOXES,
        default=DEFAULT_SANDBOX,
        help="Remote sandbox provider (default: %(default)s).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the bundle without provisioning sandboxes.",
    )
    args = parser.parse_args()

    if args.sandbox == "sail_vm" and find_spec("sail") is None:
        parser.error("Install the Sail SDK by adding --extra sail to uv run.")

    sandbox_api_key, id_root = SANDBOXES[args.sandbox]
    if args.dry_run:
        if not os.environ.get(sandbox_api_key, "").strip():
            # AgentEnv resolves provider credentials during offline preflight.
            os.environ[sandbox_api_key] = "dry-run"
    else:
        for key in (sandbox_api_key, "OPENROUTER_API_KEY"):
            if not os.environ.get(key, "").strip():
                parser.error(f"Set {key} in .env or in the environment.")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    job = ROOT / "jobs" / f"agentenv-{timestamp}-{uuid4().hex[:8]}"
    os.environ["REMOTE_EVAL_JOB_DIR"] = str(job)
    os.environ["AGENT_ENV_CONFIG"] = str(PROJECT / "config.toml")
    if args.sandbox == "daytona" and not os.environ.get("DAYTONA_API_URL", "").strip():
        os.environ["DAYTONA_API_URL"] = "https://app.daytona.io/api"

    bundle = build_bundle(job, args.suite, sandbox=args.sandbox)
    print(f"Local artifacts: {job}", flush=True)
    interrupted = 0
    try:
        if args.dry_run:
            plan = dry_run_bundle(bundle, id_root=id_root)
            for entry in plan.runs:
                print(f"Validated {plan.path(entry)}")
            return 0
        result = run_bundle(
            bundle,
            id_root=id_root,
            on_progress=lambda line: print(line, flush=True),
        )
    except BundleError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RunInterrupted as exc:
        result = exc.result
        interrupted = 128 + exc.signum

    trials = []
    for run in result.runs:
        name = Path(result.path(run.entry)).stem
        remaining = (
            []
            if run.torn_down is None
            else [sandbox.sandbox_id for sandbox in run.torn_down.still_up]
        )
        trial = {
            "task": name,
            "instance_id": run.context.instance_id,
            "sandbox_ids": [box.sandbox_id for box in run.context.deployed_sandboxes],
            "outcome": run.outcome.value,
            "scores": run.scores,
            "diagnostic": run.context.metadata.get("verifications", {})
            .get("report", {})
            .get("diagnostic"),
            "error": str(run.error) if run.error else None,
            "sandboxes_left_up": remaining,
        }
        trials.append(trial)
        print(
            f"{name}: {run.outcome.value} (report: {run.scores.get('report', 'unscored')})",
            flush=True,
        )
        if run.error:
            print(f"  {run.error}", file=sys.stderr)
        if remaining:
            print(
                f"  Sandbox teardown incomplete: {', '.join(remaining)}",
                file=sys.stderr,
            )
    (job / "eval-summary.json").write_text(
        json.dumps(
            {"suite": args.suite, "sandbox_type": args.sandbox, "trials": trials},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return interrupted or int(
        not trials
        or any(
            trial["outcome"] != Outcome.PASSED.value or trial["sandboxes_left_up"]
            for trial in trials
        )
    )


if __name__ == "__main__":
    sys.exit(main())

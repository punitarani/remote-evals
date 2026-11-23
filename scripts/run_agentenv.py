"""Run the flight evals in AgentEnv with Daytona execution and local grading."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SUITES = ("smoke", "budget", "live-smoke", "showcase")


def build_bundle(job: Path, suite: str) -> Path:
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
    for task in sorted(tasks.iterdir()):
        steps = [
            {
                "id": "sandbox",
                "type": "deploy_sandbox",
                "sandbox_name": "flight",
                "sandbox_type": "daytona",
                "sandbox_mode": "vm",
                "memory_mb": 4096,
                "ttl_seconds": 900,
            },
            {
                "id": "flight",
                "type": "run_flight_task",
                "task_name": task.name,
                "depends_on": ["sandbox"],
            },
            {
                "id": "report",
                "type": "grade_flight_report",
                "task_name": task.name,
                "depends_on": ["flight"],
            },
        ]
        (bundle / "tasks" / f"{task.name}.json").write_text(
            json.dumps(steps, indent=2) + "\n", encoding="utf-8"
        )
    return bundle


def main() -> int:
    from agent_env.bundle.parse import BundleError
    from agent_env.bundle.run import Outcome, RunInterrupted, dry_run_bundle, run_bundle

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=SUITES, default="smoke")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the bundle without provisioning Daytona.",
    )
    args = parser.parse_args()

    for key in ("DAYTONA_API_KEY", "OPENROUTER_API_KEY"):
        if not args.dry_run and not os.environ.get(key, "").strip():
            parser.error(f"Set {key} in .env or in the environment.")
    if args.dry_run and not os.environ.get("DAYTONA_API_KEY", "").strip():
        # AgentEnv resolves provider config during preflight without calling Daytona.
        os.environ["DAYTONA_API_KEY"] = "dry-run"

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    job = ROOT / "jobs" / f"agentenv-{timestamp}-{uuid4().hex[:8]}"
    os.environ["REMOTE_EVAL_JOB_DIR"] = str(job)
    os.environ["AGENT_ENV_CONFIG"] = str(ROOT / ".agentenv/config.toml")
    if not os.environ.get("DAYTONA_API_URL", "").strip():
        os.environ["DAYTONA_API_URL"] = "https://app.daytona.io/api"

    bundle = build_bundle(job, args.suite)
    print(f"Local artifacts: {job}", flush=True)
    interrupted = 0
    try:
        if args.dry_run:
            plan = dry_run_bundle(bundle, id_root="@local/remote-evals/flights")
            for entry in plan.runs:
                print(f"Validated {plan.path(entry)}")
            return 0
        result = run_bundle(
            bundle,
            id_root="@local/remote-evals/flights",
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
        json.dumps({"suite": args.suite, "trials": trials}, indent=2) + "\n",
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

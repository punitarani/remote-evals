"""Package a release, run it on Hosted Harbor, and summarize the trials."""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from agent.model import DEFAULT_MODEL

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "punitarani/remote-evals"
HUB = "https://hub.harborframework.com"
TRACE = Path("artifacts/logs/artifacts/remote-eval.json")


class LaunchError(Exception):
    pass


def staging_origin(value):
    try:
        url = urlsplit(value)
        if (
            url.scheme == "https"
            and url.hostname
            and url.username is None
            and url.port in (None, 443)
            and url.path in ("", "/")
            and not url.query
            and not url.fragment
        ):
            return f"https://{url.netloc}"
    except ValueError:
        pass
    raise LaunchError("endpoint: set STAGING_URL to the staging HTTPS origin")


def validate_health(health, release, *, dry_run=False):
    if not isinstance(health, dict) or health.get("release") != release:
        raise LaunchError("endpoint: staging release differs from the requested commit")
    if dry_run and health.get("mode") == "scripted":
        return None
    if health.get("mode") != "model":
        raise LaunchError(
            "endpoint: switch the staging service to model mode for scoring"
        )
    provider, model = health.get("provider"), health.get("model")
    if (
        provider != "openrouter"
        or not isinstance(model, str)
        or not model
        or any(char.isspace() for char in model)
    ):
        raise LaunchError("endpoint: staging did not report a valid OpenRouter model")
    return f"{provider}/{model}"


def check_staging(url, release, *, dry_run):
    try:
        health = httpx.get(f"{url}/healthz", timeout=15).raise_for_status().json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LaunchError(
            "endpoint: could not read staging /healthz over HTTPS"
        ) from exc
    return validate_health(health, release, dry_run=dry_run)


def job_config(
    target,
    codex_provider,
    release,
    url,
    tasks,
    job_name,
    *,
    codex_model=None,
    remote_model=None,
):
    agents = []
    secrets = {}
    credential_mode = "direct"
    if target in ("both", "codex"):
        codex = {
            "name": "codex",
            "kwargs": {"web_search": "disabled"},
        }
        if codex_provider == "openrouter":
            codex.update(
                model_name=f"openrouter/{DEFAULT_MODEL}",
                model_api_key_name="OPENROUTER_API_KEY",
                model_api_format="openai_responses",
                extra_allowed_hosts=["openrouter.ai"],
                secrets=["OPENROUTER_API_KEY"],
            )
            codex["kwargs"]["config"] = {
                "model_provider": "openrouter",
                "model_providers": {
                    "openrouter": {
                        "name": "OpenRouter",
                        "base_url": "https://openrouter.ai/api/v1",
                        "wire_api": "responses",
                        "env_key": "OPENROUTER_API_KEY",
                    }
                },
            }
            secrets["OPENROUTER_API_KEY"] = {"from_env": "CODEX_OPENROUTER_API_KEY"}
        else:
            if not codex_model or not codex_model.startswith("openai/"):
                raise LaunchError(
                    "Choose an openai/ model with --codex-model for the OpenAI gateway."
                )
            credential_mode = "gateway"
            codex["model_name"] = codex_model
            codex["secrets"] = ["OPENAI_API_KEY"]
            secrets["OPENAI_API_KEY"] = {"from_env": "CODEX_OPENAI_API_KEY"}
        agents.append(codex)
    if target in ("both", "remote"):
        if url is None:
            raise LaunchError("endpoint: STAGING_URL is required for the remote agent")
        adapter_env = {"STAGING_URL": url, "AGENT_RELEASE": release}
        if remote_model is not None:
            adapter_env["REMOTE_EVAL_MODEL"] = remote_model
        agents.append(
            {
                "name": "acp",
                "model_name": remote_model,
                "source": {
                    "type": "github",
                    "repo": REPOSITORY,
                    "ref": release,
                    "path": "harbor/adapter",
                    "manifest": "harbor-agent.json",
                },
                "env": adapter_env,
                "extra_allowed_hosts": [urlsplit(url).hostname],
                "secrets": ["BRIDGE_TOKEN"],
            }
        )
        secrets["BRIDGE_TOKEN"] = {"from_env": "BRIDGE_TOKEN"}
    return {
        "job_name": job_name,
        "credential_mode": credential_mode,
        "n_attempts": 1,
        "n_concurrent_trials": 4,
        "agents": agents,
        "tasks": [{"path": str(task)} for task in tasks],
        "job_secrets": secrets,
    }


def harbor_environment(config):
    # Keep the staging inference key outside the Harbor CLI process. Only these
    # source names are resolved by the per-agent job secret references.
    env = {
        name: os.environ[name]
        for name in (
            "PATH",
            "HOME",
            "TMPDIR",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HARBOR_API_KEY",
        )
        if name in os.environ
    }
    for secret in config["job_secrets"].values():
        name = secret["from_env"]
        value = os.environ.get(name, "").strip()
        if not value:
            raise LaunchError(
                f"credentials: set {name} in the local environment or staging secrets"
            )
        env[name] = value
    env.update(NO_COLOR="1", COLUMNS="160")
    return env


def harbor(args, env, *, timeout=300):
    try:
        result = subprocess.run(
            ["harbor", *args],
            env=env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LaunchError(
            "Harbor CLI is missing; run the launcher with uv run"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LaunchError(f"Harbor {args[0]} timed out") from exc

    output = result.stdout
    if result.returncode or "--json" not in args:
        output += result.stderr
    for name, value in env.items():
        if value and (name.endswith("_KEY") or name.endswith("_TOKEN")):
            output = output.replace(value, "[redacted]")
    if result.returncode:
        raise LaunchError(
            f"Harbor {args[0]} exited with code {result.returncode}\n{output.strip()}"
        )
    return output


def checked_out_release(requested):
    try:
        release = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise LaunchError("commit the release before running a hosted eval") from exc
    if requested and requested != release:
        raise LaunchError("check out the full requested release SHA before launching")
    return release


def package_tasks(release, suite, scratch):
    # Read inputs directly from Git so an edited or untracked fixture cannot
    # produce different task packages under the same release SHA.
    try:
        archive = subprocess.check_output(
            ["git", "archive", release, "harbor/task", "pyproject.toml", "uv.lock"],
            cwd=ROOT,
        )
        with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
            tree.extractall(scratch, filter="data")
        target = scratch / "tasks"
        subprocess.run(
            [sys.executable, "harbor/task/package.py", str(target), "--suite", suite],
            cwd=scratch,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise LaunchError("task packaging failed for the requested release") from exc
    tasks = sorted(path.parent for path in target.glob("*/task.toml"))
    if len(tasks) != 1:
        raise LaunchError("task packager produced an unexpected number of tasks")
    return tasks


def submit(config, scratch, env, *, dry_run):
    config_path = scratch / "launch.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    flags = ["--dry-run"] if dry_run else ["--yes"]
    output = harbor(["run", "--launch", "-c", str(config_path), *flags], env)
    print(output.strip(), flush=True)
    if dry_run:
        return None
    match = re.search(r"Launched job ([0-9a-f-]{36})\b", output)
    if match is None:
        raise LaunchError(
            "Harbor returned no job ID; check the Hub before retrying the launch"
        )
    return str(UUID(match[1]))


def validate_adapter_source(config, scratch, env):
    adapter = next(
        (agent for agent in config["agents"] if agent["name"] == "acp"), None
    )
    if adapter is None:
        return
    # A local-task dry run stops before Hub source resolution. A registered
    # task lets Harbor validate the private ACP repo without uploading or running anything.
    source_check = {
        **config,
        "job_name": f"{config['job_name']}-source",
        "agents": [adapter],
        "tasks": [],
        "datasets": [{"name": "harbor/hello-world", "n_tasks": 1}],
        "job_secrets": {"BRIDGE_TOKEN": config["job_secrets"]["BRIDGE_TOKEN"]},
        "credential_mode": "direct",
    }
    submit(source_check, scratch, env, dry_run=True)


def wait_for_job(job_id, env):
    deadline = time.monotonic() + 30 * 60
    previous = None
    failed_polls = 0
    while time.monotonic() < deadline:
        try:
            status = json.loads(
                harbor(["hub", "job", "status", job_id, "--json"], env, timeout=30)
            )
        except LaunchError:
            failed_polls += 1
            if failed_polls == 3:
                raise
            time.sleep(10)
            continue
        failed_polls = 0
        if status != previous:
            print(
                f"{status['status']} · {status['completed']}/{status['total']} completed · "
                f"{status['failed']} failed · {status['canceled']} canceled",
                flush=True,
            )
            previous = status
        if status["status"] in {"completed", "failed", "canceled"}:
            return status
        time.sleep(10)
    raise LaunchError("Harbor job still running after 30 minutes; follow the Hub link")


def trial_outcome(result, trace, release):
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    reward = rewards.get("reward")
    error_type = (
        (result.get("exception_info") or {}).get("exception_type") or ""
    ).lower()
    if trace is not None:
        if trace.get("status") == "cancelled":
            return "canceled", reward
        if trace.get("status") != "completed":
            error = str(trace.get("error_code") or "")
            if error == "invalid_trace":
                return "bridge trace invalid", reward
            if error == "inference":
                return "inference", reward
            if error == "endpoint" or error.startswith("endpoint_"):
                return "endpoint", reward
            if error == "budget":
                return "agent budget", reward
            return "bridge", reward
        if trace.get("release") != release or trace.get("mode") != "model":
            return "endpoint", reward
    if error_type:
        if any(
            word in error_type for word in ("verifier", "rewardfile", "rewardparse")
        ):
            return "verifier", reward
        if any(
            word in error_type
            for word in ("inference", "credential", "api", "rate", "contextwindow")
        ):
            return "inference", reward
        return "infrastructure", reward
    if (result.get("config") or {}).get("agent", {}).get(
        "name"
    ) == "acp" and trace is None:
        return "bridge evidence missing", reward
    if reward is None:
        return "verifier", reward
    return ("pass" if reward == 1 else "task score"), reward


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_bridge_trace(path):
    if not path.is_file():
        return None
    try:
        trace = read_json(path)
    except OSError, ValueError:
        trace = None
    if not isinstance(trace, dict):
        return {"status": "error", "error_code": "invalid_trace"}
    return trace


def trajectory_issue(path, trace):
    try:
        trajectory = read_json(path)
        if trajectory["session_id"] != trace["trial_id"]:
            return "trajectory session mismatch"
        steps = trajectory["steps"]
        if not isinstance(steps, list) or not steps:
            return "trajectory invalid"
        calls = []
        observed = set()
        for step in steps:
            for call in step.get("tool_calls") or []:
                calls.append((call["tool_call_id"], call["function_name"]))
            for result in (step.get("observation") or {}).get("results") or []:
                if result.get("content"):
                    observed.add(result["source_call_id"])
        expected_calls = [
            (action["action_id"], action["name"])
            for action in trace["actions"]
            if action["status"] == "completed"
        ]
        expected_ids = {action_id for action_id, _ in expected_calls}
        if (
            len(expected_ids) != len(trace["actions"])
            or calls != expected_calls
            or observed != expected_ids
        ):
            return "trajectory tool trace mismatch"
    except FileNotFoundError:
        return "trajectory missing"
    except AttributeError, KeyError, OSError, TypeError, ValueError:
        return "trajectory invalid"
    return None


def read_trials(job_dir, release):
    trials = []
    for result_path in sorted(job_dir.glob("*/result.json")):
        result = read_json(result_path)
        trace = read_bridge_trace(result_path.parent / TRACE)
        outcome, reward = trial_outcome(result, trace, release)
        issue = None
        if trace is not None and outcome in {"pass", "task score"}:
            issue = trajectory_issue(
                result_path.parent / "agent/trajectory.json", trace
            )
        agent = result.get("agent_info") or {}
        model = agent.get("model_info") or {}
        trials.append(
            {
                "id": result["id"],
                "task": result["task_name"],
                "task_ref": (result.get("task_id") or {}).get("ref"),
                "task_checksum": result.get("task_checksum"),
                "agent": agent.get("name"),
                "agent_version": agent.get("version"),
                "provider": (trace or {}).get("provider") or model.get("provider"),
                "model": (trace or {}).get("model") or model.get("name"),
                "reward": reward,
                "outcome": outcome,
                "trajectory_issue": issue,
                "error_type": (result.get("exception_info") or {}).get(
                    "exception_type"
                ),
                "bridge": trace,
            }
        )
    return trials


def write_summary(message):
    print(message, flush=True)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as summary:
            summary.write(f"{message}\n\n")


def summarize(job_id, job_dir, release, status, fixture_revisions):
    trials = read_trials(job_dir, release)
    summary = {
        "job_id": job_id,
        "viewer_url": f"{HUB}/jobs/{job_id}",
        "release": release,
        "fixture_revisions": fixture_revisions,
        "status": status,
        "trials": trials,
    }
    (job_dir / "eval-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    rows = [
        "| Task | Agent | Provider | Model | Trial ID | Reward | Outcome | Trajectory issue |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for trial in trials:
        values = [
            trial[key]
            for key in (
                "task",
                "agent",
                "provider",
                "model",
                "id",
                "reward",
                "outcome",
                "trajectory_issue",
            )
        ]
        columns = [
            str(value).replace("|", "\\|").replace("\n", " ")
            if value is not None
            else "—"
            for value in values
        ]
        rows.append("| " + " | ".join(columns) + " |")
    if len(trials) != status["total"]:
        rows.append(
            f"\nDownloaded results for {len(trials)} of {status['total']} trials."
        )
    write_summary("\n".join(rows))
    return (
        status["status"] == "completed"
        and len(trials) == status["total"]
        and all(
            trial["outcome"] == "pass" and trial["trajectory_issue"] is None
            for trial in trials
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("smoke", "live-smoke"), default="smoke")
    parser.add_argument("--target", choices=("both", "codex", "remote"), default="both")
    parser.add_argument(
        "--codex-provider", choices=("openrouter", "openai"), default="openrouter"
    )
    parser.add_argument(
        "--codex-model",
        default=os.getenv("CODEX_MODEL"),
        help="Explicit openai/ model for the OpenAI gateway",
    )
    parser.add_argument("--release", help="Expected full commit SHA, defaults to HEAD")
    parser.add_argument("--jobs-dir", type=Path, default=ROOT / "jobs")
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate without queuing trials"
    )
    args = parser.parse_args(argv)

    try:
        release = checked_out_release(args.release)
        url = (
            None
            if args.target == "codex"
            else staging_origin(os.environ.get("STAGING_URL", ""))
        )
        remote_model = (
            check_staging(url, release, dry_run=args.dry_run) if url else None
        )
        now = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"remote-evals-{args.suite}-{release[:8]}-{now}-{uuid4().hex[:6]}"
        with tempfile.TemporaryDirectory(prefix="remote-evals-") as temporary:
            scratch = Path(temporary)
            tasks = package_tasks(release, args.suite, scratch)
            fixture_revisions = {}
            for task in tasks:
                with (task / "task.toml").open("rb") as manifest:
                    metadata = tomllib.load(manifest)["metadata"]
                if digest := metadata.get("fixture_sha256"):
                    fixture_revisions[task.name] = digest
            config = job_config(
                args.target,
                args.codex_provider,
                release,
                url,
                tasks,
                name,
                codex_model=args.codex_model,
                remote_model=remote_model,
            )
            env = harbor_environment(config)
            validate_adapter_source(config, scratch, env)
            job_id = submit(config, scratch, env, dry_run=args.dry_run)
        if job_id is None:
            return 0
        write_summary(f"[Hosted Harbor job]({HUB}/jobs/{job_id}) · Release `{release}`")
        status = wait_for_job(job_id, env)
        destination = args.jobs_dir.resolve() / job_id
        print(
            harbor(
                ["jobs", "download", job_id, "--output-dir", str(destination)], env
            ).strip()
        )
        passed = summarize(
            job_id, destination / name, release, status, fixture_revisions
        )
        return 0 if passed else 1
    except (LaunchError, OSError, ValueError) as exc:
        print(f"Eval failed: {exc}", file=sys.stderr)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            write_summary(
                "The eval launcher failed. See the workflow log for the error."
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

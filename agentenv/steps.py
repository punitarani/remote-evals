"""AgentEnv task steps for sandboxed flight tools and independent report grading."""

import asyncio
import base64
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

import httpx
from agent_env.providers.sandbox_providers.sandbox_provider import (
    build_sandbox_provider,
)
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.task_step import TaskStep
from agent_env.task_step.task_steps.verifiers.verifier import (
    Verification,
    VerifierTaskStep,
)

from agent.harness import run_agent
from agent.model import DEFAULT_MODEL, OpenRouter
from agent.tools import BASH_OUTPUT_LIMIT, BASH_TIMEOUT

RUN_TIMEOUT = 180
IMAGE = "remote-evals:flight"
CONTAINER = "flight-workspace"
FIXTURE_PYTHON = "/opt/fixture/.venv/bin/python"
# Matches the maximum input accepted by the shared verifier. Capture one extra
# byte so an oversized report still reaches the verifier as oversized.
REPORT_READ_BYTES = 16_385
CONTAINER_FLAGS = ("--cap-drop=ALL", "--security-opt=no-new-privileges")
BASH_WRAPPER = r"""
output=$(mktemp) || exit 125
trap 'rm -f -- "$output"' EXIT
timeout --kill-after=2s "$1" /bin/bash -c '
    /bin/bash -c "$1" 2>&1 | tail -c "$2"
    exit "${PIPESTATUS[0]}"
' bash "$2" "$3" > "$output"
status=$?
base64 < "$output" | tr -d '\n'
exit "$status"
"""
READ_REPORT = """
set -o pipefail
if [[ ! -L report.json && -f report.json ]]; then
    head -c "$1" -- report.json | base64 -w0
else
    exit 3
fi
"""


def task_paths(task_name):
    job = Path(os.environ["REMOTE_EVAL_JOB_DIR"])
    return job / "tasks" / task_name, job / "results" / task_name


async def docker(sandbox, *args, timeout=45):
    async with asyncio.timeout(timeout):
        code, output, error = await sandbox.exec_with_output("sudo", "docker", *args)
    if code < 0:
        raise RuntimeError(
            f"Daytona lost the Docker {args[0]} command: {error.strip()}"
        )
    return code, output, error


async def docker_checked(sandbox, *args, timeout=45):
    code, output, error = await docker(sandbox, *args, timeout=timeout)
    if code:
        raise RuntimeError(
            f"Docker {args[0]} exited {code}: {(error or output)[-3000:]}"
        )


async def build_image(sandbox, task):
    with tempfile.TemporaryDirectory() as scratch:
        archive_path = Path(scratch) / "image.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for path in sorted((task / "environment").iterdir()):
                archive.add(path, arcname=path.name)
            archive.add(
                Path(__file__).with_name("fli_client.py"),
                arcname="payload/fli_client.py",
            )
        await sandbox.write_host_file(
            archive_path.read_bytes(), "/tmp/flight-image.tar.gz"
        )
    await sandbox.exec_script(
        "mkdir -p /tmp/flight-image && tar -xzf /tmp/flight-image.tar.gz -C /tmp/flight-image"
    )
    await docker_checked(
        sandbox, "build", "-t", IMAGE, "/tmp/flight-image", timeout=300
    )
    await docker_checked(
        sandbox,
        "run",
        "-d",
        "--network=none",
        *CONTAINER_FLAGS,
        "--name",
        CONTAINER,
        IMAGE,
        "sleep",
        "infinity",
    )


async def collect_report(sandbox, artifacts):
    code, output, _ = await docker(
        sandbox,
        "exec",
        "-w",
        "/workspace",
        CONTAINER,
        "bash",
        "-c",
        READ_REPORT,
        "bash",
        str(REPORT_READ_BYTES),
    )
    if code == 0:
        # Daytona's command session appends a newline to stdout.
        (artifacts / "report.json").write_bytes(
            base64.b64decode(output.rstrip("\r\n"), validate=True)
        )
    elif code != 3:
        raise RuntimeError(f"Could not read the trial report (exit {code}).")


class FlightTools:
    def __init__(self, sandbox, manifest, trace):
        self.sandbox = sandbox
        self.server = manifest["environment"]["mcp_servers"][0]
        self.network = (
            "none" if manifest["metadata"]["fli_mode"] == "emulated" else "bridge"
        )
        self.trace = trace

    async def execute(self, name, arguments):
        action = {"name": name, "status": "failed"}
        self.trace["actions"].append(action)
        if name == "bash":
            result = await self.bash(arguments["command"])
            action.update(status="completed", exit_code=result["exit_code"])
        else:
            result = await self.fli(name, arguments)
            action.update(status="completed", success=result.get("success") is True)
            rows = result.get("dates" if name == "search_dates" else "flights")
            if isinstance(rows, list):
                action["results"] = len(rows)
        return result

    async def bash(self, command):
        code, output, _ = await docker(
            self.sandbox,
            "exec",
            "-w",
            "/workspace",
            CONTAINER,
            "bash",
            "-c",
            BASH_WRAPPER,
            "bash",
            str(BASH_TIMEOUT),
            command,
            str(BASH_OUTPUT_LIMIT + 1),
        )
        output = base64.b64decode(output.rstrip("\r\n"), validate=True)
        return {
            "exit_code": code,
            "output": output[-BASH_OUTPUT_LIMIT:].decode(errors="replace"),
            "truncated": len(output) > BASH_OUTPUT_LIMIT,
        }

    async def fli(self, name, arguments):
        code, output, _ = await docker(
            self.sandbox,
            "run",
            "--rm",
            f"--network={self.network}",
            *CONTAINER_FLAGS,
            IMAGE,
            "timeout",
            "--kill-after=2s",
            str(BASH_TIMEOUT),
            FIXTURE_PYTHON,
            "/opt/fixture/fli_client.py",
            name,
            json.dumps(arguments),
            self.server["command"],
            *self.server["args"],
        )
        if code:
            return {"success": False, "error": "Fli could not complete the search."}
        return json.loads(output)


class RunFlightTask(TaskStep):
    type = "run_flight_task"
    entity_refs = ()

    def __init__(self, *, task_name, **kwargs):
        super().__init__(**kwargs)
        self.task_name = task_name

    @classmethod
    def from_dict(cls, data):
        return cls(**cls._base_from_dict(data), task_name=data["task_name"])

    def to_dict(self):
        return {**super().to_dict(), "task_name": self.task_name}

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        task, artifacts = task_paths(self.task_name)
        artifacts.mkdir(parents=True)
        manifest = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))
        (deployed,) = context.deployed_sandboxes
        provider = build_sandbox_provider(deployed.sandbox_type)
        model = os.environ.get("AGENT_MODEL", "").strip() or DEFAULT_MODEL
        trace = {
            "sandbox_id": deployed.sandbox_id,
            "sandbox_type": deployed.sandbox_type,
            "provider": "openrouter",
            "model": model,
            **manifest["metadata"],
            "status": "failed",
            "actions": [],
        }
        try:
            sandbox = await provider.get_sandbox(deployed.sandbox_id)
            await build_image(sandbox, task)
            try:
                async with (
                    httpx.AsyncClient(timeout=40) as client,
                    asyncio.timeout(RUN_TIMEOUT),
                ):
                    await run_agent(
                        (task / "instruction.md").read_text(encoding="utf-8"),
                        OpenRouter(client, os.environ["OPENROUTER_API_KEY"], model),
                        FlightTools(sandbox, manifest, trace),
                    )
            except Exception:
                # A completed Bash action may have written the report before inference failed.
                try:
                    await collect_report(sandbox, artifacts)
                except Exception as exc:
                    trace["report_capture_error"] = type(exc).__name__
                raise
            await collect_report(sandbox, artifacts)
            trace["status"] = "completed"
            context.record_model("solver", self.id, f"openrouter/{model}")
        finally:
            try:
                await provider.close()
            finally:
                (artifacts / "trace.json").write_text(
                    json.dumps(trace, indent=2) + "\n", encoding="utf-8"
                )
        return context


class GradeFlightReport(VerifierTaskStep):
    type = "grade_flight_report"
    entity_refs = ()

    def __init__(self, *, task_name, **kwargs):
        super().__init__(**kwargs)
        self.task_name = task_name

    @classmethod
    def from_dict(cls, data):
        return cls(**cls._base_from_dict(data), task_name=data["task_name"])

    def to_dict(self):
        return {**super().to_dict(), "task_name": self.task_name}

    async def verify(self, context: TaskStepContext) -> Verification:
        task, artifacts = task_paths(self.task_name)
        subprocess.run(
            [
                sys.executable,
                str(task / "tests" / "verify.py"),
                str(artifacts / "report.json"),
                str(artifacts),
            ],
            env={},
            timeout=30,
            check=True,
        )
        score = float((artifacts / "reward.txt").read_text(encoding="utf-8"))
        diagnostic = (artifacts / "diagnostic.txt").read_text(encoding="utf-8").strip()
        return Verification(score=score, output={"diagnostic": diagnostic})

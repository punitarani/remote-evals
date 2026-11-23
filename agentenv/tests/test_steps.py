import base64
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent.model import InferenceError
from scripts.agentenv_steps import BASH_WRAPPER, RunFlightTask


class FlightStepTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which("timeout"), "requires GNU timeout")
    def test_bash_deadline_covers_background_processes_holding_output(self):
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                BASH_WRAPPER,
                "bash",
                "0.1",
                "sleep 5 & printf ready",
                "128",
            ],
            capture_output=True,
            timeout=3,
            check=False,
        )
        self.assertEqual(result.returncode, 124)

    @unittest.skipUnless(shutil.which("timeout"), "requires GNU timeout")
    def test_bash_keeps_the_bounded_output_bytes_and_command_exit_code(self):
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                BASH_WRAPPER,
                "bash",
                "1",
                r"printf '123\377\000'; exit 7",
                "3",
            ],
            capture_output=True,
            timeout=3,
            check=False,
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(base64.b64decode(result.stdout, validate=True), b"3\xff\x00")

    async def test_report_is_retrieved_if_inference_fails_after_writing_it(self):
        report = b'{"status":"no_match"}'
        sandbox = SimpleNamespace(
            exec_with_output=AsyncMock(
                return_value=(0, base64.b64encode(report).decode() + "\n", "")
            )
        )
        provider = SimpleNamespace(
            get_sandbox=AsyncMock(return_value=sandbox), close=AsyncMock()
        )
        context = SimpleNamespace(
            deployed_sandboxes=[
                SimpleNamespace(sandbox_type="daytona", sandbox_id="trial")
            ]
        )
        with tempfile.TemporaryDirectory() as job_dir:
            job = Path(job_dir)
            task = job / "tasks" / "under-budget"
            task.mkdir(parents=True)
            (task / "instruction.md").write_text("Write report.json.")
            (task / "task.toml").write_text(
                '[metadata]\nfli_mode = "emulated"\n'
                '[[environment.mcp_servers]]\ncommand = "fixture"\nargs = []\n'
            )
            step = RunFlightTask(id="flight", version=None, task_name=task.name)
            with (
                patch.dict(
                    "os.environ",
                    {"REMOTE_EVAL_JOB_DIR": job_dir, "OPENROUTER_API_KEY": "test"},
                ),
                patch(
                    "scripts.agentenv_steps.build_sandbox_provider",
                    return_value=provider,
                ),
                patch("scripts.agentenv_steps.build_image", new_callable=AsyncMock),
                patch(
                    "scripts.agentenv_steps.run_agent",
                    new_callable=AsyncMock,
                    side_effect=InferenceError("Could not reach OpenRouter."),
                ),
            ):
                with self.assertRaisesRegex(
                    InferenceError, "Could not reach OpenRouter"
                ):
                    await step.execute(context)

            artifacts = job / "results" / task.name
            self.assertEqual((artifacts / "report.json").read_bytes(), report)
            trace = json.loads((artifacts / "trace.json").read_text())
            self.assertEqual(trace["status"], "failed")
            provider.close.assert_awaited_once()

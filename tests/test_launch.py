import json
from pathlib import Path

import pytest

from scripts import run_eval

RELEASE = "a" * 40
URL = "https://staging.example.org"


def test_hosted_configuration_routes_each_secret_only_to_its_agent():
    config = run_eval.job_config(
        "both", "openrouter", RELEASE, URL, [Path("case")], "test"
    )
    codex, adapter = config["agents"]

    assert config["credential_mode"] == "direct"
    assert config["job_secrets"] == {
        "OPENROUTER_API_KEY": {"from_env": "CODEX_OPENROUTER_API_KEY"},
        "BRIDGE_TOKEN": {"from_env": "BRIDGE_TOKEN"},
    }
    assert codex["secrets"] == ["OPENROUTER_API_KEY"]
    assert codex["model_name"] == "openrouter/openai/gpt-5.6-luna"
    assert codex["extra_allowed_hosts"] == ["openrouter.ai"]
    assert adapter["secrets"] == ["BRIDGE_TOKEN"]
    assert adapter["source"]["ref"] == RELEASE
    assert adapter["extra_allowed_hosts"] == ["staging.example.org"]


def test_codex_fallback_uses_gateway_and_needs_no_staging_url():
    config = run_eval.job_config(
        "codex", "openai", RELEASE, None, [Path("case")], "test"
    )

    assert config["credential_mode"] == "gateway"
    assert config["job_secrets"] == {
        "OPENAI_API_KEY": {"from_env": "CODEX_OPENAI_API_KEY"}
    }
    assert config["agents"][0]["model_name"] == "openai/gpt-5.6-luna"


def test_release_and_model_mode_are_checked_before_queueing():
    with pytest.raises(run_eval.LaunchError, match="release"):
        run_eval.validate_health({"release": "b" * 40, "mode": "model"}, RELEASE)

    with pytest.raises(run_eval.LaunchError, match="model mode"):
        run_eval.validate_health({"release": RELEASE, "mode": "scripted"}, RELEASE)

    run_eval.validate_health(
        {"release": RELEASE, "mode": "scripted"}, RELEASE, dry_run=True
    )


@pytest.mark.parametrize(
    "value", ["http://example.org", "https://example.org?query=1", "https://a@b"]
)
def test_rejects_invalid_staging_origins(value):
    with pytest.raises(run_eval.LaunchError, match="HTTPS origin"):
        run_eval.staging_origin(value)


def test_classifies_low_score_and_infrastructure_separately():
    result = {"verifier_result": {"rewards": {"reward": 0}}}
    trace = {"release": RELEASE, "mode": "model", "status": "completed"}

    assert run_eval.trial_outcome(result, trace, RELEASE) == ("task score", 0)
    assert run_eval.trial_outcome(
        result, {**trace, "status": "error", "error_code": "inference"}, RELEASE
    ) == (
        "inference",
        0,
    )
    assert run_eval.trial_outcome(
        result,
        {**trace, "status": "error", "error_code": "endpoint_unreachable"},
        RELEASE,
    ) == (
        "endpoint",
        0,
    )
    verifier_error = {"exception_info": {"exception_type": "VerifierTimeoutError"}}
    assert run_eval.trial_outcome(verifier_error, None, RELEASE) == ("verifier", None)


def test_summary_reads_harbors_archive_layout_and_retains_provenance(tmp_path):
    trial = tmp_path / "cheapest-nonstop__trial"
    trace = trial / "artifacts/logs/artifacts/remote-eval.json"
    trace.parent.mkdir(parents=True)
    trace.write_text(
        json.dumps({"release": RELEASE, "mode": "model", "status": "completed"})
    )
    (trial / "result.json").write_text(
        json.dumps(
            {
                "id": "trial-id",
                "task_name": "cheapest-nonstop",
                "task_id": {"ref": "sha256:task-digest"},
                "config": {"agent": {"name": "acp"}},
                "verifier_result": {"rewards": {"reward": 1}},
            }
        )
    )
    status = {"status": "completed", "total": 1}
    revisions = {"cheapest-nonstop": "fixture-digest"}

    assert run_eval.summarize("job-id", tmp_path, RELEASE, status, revisions)
    report = json.loads((tmp_path / "eval-summary.json").read_text())
    assert report["release"] == RELEASE
    assert report["fixture_revisions"] == revisions
    assert report["trials"][0]["id"] == "trial-id"
    assert report["trials"][0]["task_ref"] == "sha256:task-digest"

    trace.unlink()
    assert not run_eval.summarize("job-id", tmp_path, RELEASE, status, revisions)

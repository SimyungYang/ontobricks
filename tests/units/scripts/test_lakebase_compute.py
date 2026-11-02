"""The deploy keeps the Lakebase primary endpoint at the configured compute.

``scripts/bootstrap/lakebase-compute.sh`` reads the read-write endpoint of
``projects/<project>/branches/<branch>`` and, when its autoscaling range or
scale-to-zero timeout differs from the deploy config, updates only those
fields. A fake ``databricks`` CLI records the calls.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "bootstrap" / "lakebase-compute.sh"
CONFIG = REPO_ROOT / "scripts" / "deploy.config.sh"

pytestmark = pytest.mark.unit

_FAKE_CLI = """#!/usr/bin/env bash
echo "$*" >> "$CALLS"
if [[ "$1 $2" == "postgres list-endpoints" ]]; then
    cat "$ENDPOINTS"
elif [[ "$1 $2" == "postgres update-endpoint" ]]; then
    echo '{}'
else
    exit 2
fi
"""


def _endpoint(min_cu=1, max_cu=4, suspend=None, kind="ENDPOINT_TYPE_READ_WRITE", name="primary"):
    status = {"endpoint_type": kind, "autoscaling_limit_min_cu": min_cu, "autoscaling_limit_max_cu": max_cu}
    if suspend:
        status["suspend_timeout_duration"] = suspend
    return {"name": f"projects/p/branches/production/endpoints/{name}", "status": status}


def _run(tmp_path: Path, endpoints: list, *args: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    cli = bin_dir / "databricks"
    cli.write_text(_FAKE_CLI)
    cli.chmod(0o755)
    (tmp_path / "endpoints.json").write_text(json.dumps(endpoints))
    calls = tmp_path / "calls.txt"
    calls.write_text("")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "CALLS": str(calls), "ENDPOINTS": str(tmp_path / "endpoints.json")}
    done = subprocess.run(["bash", str(SCRIPT), "--project", "p", "--branch", "production", *args],
                          cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)
    updates = [line for line in calls.read_text().splitlines() if line.startswith("postgres update-endpoint")]
    return done, updates


def _defaults() -> list:
    return ["--min-cu", "4", "--max-cu", "16", "--suspend-timeout", "3600s"]


def test_updates_the_read_write_endpoint_when_compute_differs(tmp_path):
    eps = [_endpoint(kind="ENDPOINT_TYPE_READ_ONLY", name="ro"), _endpoint()]
    done, updates = _run(tmp_path, eps, *_defaults())
    assert done.returncode == 0, done.stderr
    assert len(updates) == 1
    update = updates[0]
    assert "projects/p/branches/production/endpoints/primary " in update
    assert "spec.autoscaling_limit_min_cu,spec.autoscaling_limit_max_cu,spec.suspend_timeout_duration" in update
    body = json.loads(update.split("--json ", 1)[1].split(" -o", 1)[0])
    assert body == {"spec": {"autoscaling_limit_min_cu": 4, "autoscaling_limit_max_cu": 16,
                             "suspend_timeout_duration": "3600s"}}


def test_leaves_a_matching_endpoint_alone(tmp_path):
    done, updates = _run(tmp_path, [_endpoint(4, 16, "3600s")], *_defaults())
    assert done.returncode == 0, done.stderr
    assert updates == []
    assert "already" in done.stdout


def test_empty_values_skip_their_fields(tmp_path):
    done, updates = _run(tmp_path, [_endpoint(1, 4)], "--min-cu", "", "--max-cu", "", "--suspend-timeout", "1800s")
    assert done.returncode == 0, done.stderr
    assert len(updates) == 1
    assert " spec.suspend_timeout_duration " in updates[0]
    assert "autoscaling_limit" not in updates[0]


def test_all_empty_is_a_no_op(tmp_path):
    done, updates = _run(tmp_path, [_endpoint()], "--min-cu", "", "--max-cu", "", "--suspend-timeout", "")
    assert done.returncode == 0, done.stderr
    assert updates == []


def test_dry_run_reports_without_updating(tmp_path):
    done, updates = _run(tmp_path, [_endpoint()], *_defaults(), "--dry-run")
    assert done.returncode == 0, done.stderr
    assert updates == []
    assert "would set" in done.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--min-cu", "2", "--max-cu", "20"],  # range wider than 16 CU
        ["--min-cu", "8", "--max-cu", "4"],  # max below min
        ["--min-cu", "four"],
        ["--suspend-timeout", "1h"],
    ],
)
def test_rejects_invalid_settings_before_calling_the_api(tmp_path, args):
    done, updates = _run(tmp_path, [_endpoint()], *args)
    assert done.returncode != 0
    assert updates == []


def test_deploy_config_defaults_to_4_to_16_cu_and_one_hour():
    text = CONFIG.read_text(encoding="utf-8")
    assert 'DEFAULT_LAKEBASE_MIN_CU="${DEFAULT_LAKEBASE_MIN_CU-4}"' in text
    assert 'DEFAULT_LAKEBASE_MAX_CU="${DEFAULT_LAKEBASE_MAX_CU-16}"' in text
    assert 'DEFAULT_LAKEBASE_SUSPEND_TIMEOUT="${DEFAULT_LAKEBASE_SUSPEND_TIMEOUT-3600s}"' in text


def test_deploy_applies_the_compute_after_provisioning():
    deploy = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    provision = deploy.index("scripts/bootstrap/setup-lakebase.sh \"${_setup_args[@]}\"")
    compute = deploy.index('scripts/bootstrap/lakebase-compute.sh "${_compute_args[@]}"')
    assert compute > provision

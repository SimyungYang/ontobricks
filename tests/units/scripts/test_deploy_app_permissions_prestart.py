"""The app self-permissions are restored before the apps start, not only after.

``databricks bundle deploy`` rewrites the app ACL from ``databricks.yml``
``permissions:`` and drops the app service principal's CAN_MANAGE on its own
app. If the grant is only restored after ``bundle run``, everyone opening the
freshly restarted app lands on ``/access-denied?reason=bootstrap`` until it is.
deploy.sh therefore runs the idempotent bootstrap between ``bundle deploy``
and ``bundle run`` (step 6c) and keeps the post-start run as a backstop.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "scripts" / "deploy.sh"

PRESTART_LABEL = '"App self-permissions (pre-start)"'
BACKSTOP_LABEL = '"App self-permissions (backstop)"'


def _body() -> str:
    return DEPLOY.read_text(encoding="utf-8")


def _helper_source() -> str:
    """Return the ``grant_app_self_permissions`` definition from deploy.sh."""
    match = re.search(r"^grant_app_self_permissions\(\) \{\n.*?^\}\n", _body(), re.M | re.S)
    assert match, "grant_app_self_permissions() not found in deploy.sh"
    return match.group(0)


def _run_helper(tmp_path: Path, bootstrap_exit: int) -> subprocess.CompletedProcess:
    """Run the real helper against a fake bootstrap script in ``tmp_path``."""
    fake = tmp_path / "scripts" / "bootstrap" / "app-permissions.sh"
    fake.parent.mkdir(parents=True)
    fake.write_text(f'#!/usr/bin/env bash\necho "called with: $*"\nexit {bootstrap_exit}\n')
    script = (
        "set -euo pipefail\n"
        'begin_step() { echo "STEP $1"; }\n'
        'ok() { echo "OK $*"; }\n'
        'warn() { echo "WARN $*"; }\n'
        "APP_NAME=ontobricks-09x\nMCP_APP_NAME=mcp-ontobricks-09x\n"
        f"{_helper_source()}"
        'grant_app_self_permissions "label" "granted" "retry later"\n'
        'echo "CONTINUED"\n'
    )
    return subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )


class TestPreStartWiring:
    def test_prestart_grant_runs_after_the_last_bundle_deploy(self):
        body = _body()
        assert body.index(PRESTART_LABEL) > body.rindex("\n    databricks bundle deploy ")

    def test_prestart_grant_runs_before_the_apps_start(self):
        body = _body()
        assert body.index(PRESTART_LABEL) < body.index("\n    databricks bundle run ")

    def test_prestart_grant_honours_no_bootstrap(self):
        body = _body()
        guard = body.rindex("if $DO_BOOTSTRAP; then", 0, body.index(PRESTART_LABEL))
        assert body.index(PRESTART_LABEL) - guard < 120

    def test_post_start_backstop_is_kept(self):
        body = _body()
        assert body.index(BACKSTOP_LABEL) > body.rindex("databricks bundle run ")


class TestGrantHelper:
    def test_passes_both_app_names_to_the_bootstrap(self, tmp_path):
        result = _run_helper(tmp_path, 0)
        assert "called with: ontobricks-09x mcp-ontobricks-09x" in result.stdout

    def test_success_reports_ok(self, tmp_path):
        result = _run_helper(tmp_path, 0)
        assert "OK granted" in result.stdout

    def test_failure_warns_and_the_deploy_continues(self, tmp_path):
        result = _run_helper(tmp_path, 1)
        assert result.returncode == 0
        assert "WARN retry later" in result.stdout
        assert "CONTINUED" in result.stdout

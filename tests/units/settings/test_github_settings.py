"""Tests for Settings → GitHub (GitHubSettings)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from back.core.errors import ValidationError
from back.objects.domain.settings.GitHubSettings import GitHubSettings

_SS = "back.objects.domain.settings.SettingsService"


@pytest.fixture
def gcs():
    store = {"github_config": {}}
    svc = MagicMock()
    svc.get_github_config.side_effect = lambda *a: dict(store["github_config"])

    def _set(host, token, registry_cfg, cfg):
        store["github_config"] = cfg
        return True, "ok"

    svc.set_github_config.side_effect = _set
    with patch(f"{_SS}.global_config_service", svc), patch(
        f"{_SS}.SettingsService._resolve_context",
        return_value=(MagicMock(), "h", "t", {"catalog": "c", "schema": "s"}),
    ):
        yield store


def test_save_assigns_stable_instance_uuid(gcs):
    out = GitHubSettings.save_github_result(
        {"enabled": True, "owner": "acme", "repo": "onto"}, MagicMock(), MagicMock()
    )
    uuid1 = out["github"]["instance_uuid"]
    assert len(uuid1) == 32
    out = GitHubSettings.save_github_result({"branch": "dev"}, MagicMock(), MagicMock())
    assert out["github"]["instance_uuid"] == uuid1
    assert out["github"]["branch"] == "dev"
    assert out["github"]["owner"] == "acme"


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": True, "owner": "", "repo": "onto"},
        {"owner": "acme org", "repo": "onto"},
        {"owner": "acme", "repo": "onto", "base_path": "a/../b"},
    ],
)
def test_save_rejects_invalid_settings(gcs, payload):
    with pytest.raises(ValidationError):
        GitHubSettings.save_github_result(payload, MagicMock(), MagicMock())


def test_token_is_never_part_of_the_config(gcs):
    out = GitHubSettings.save_github_result(
        {"owner": "acme", "repo": "onto", "token": "ghp_secret"}, MagicMock(), MagicMock()
    )
    assert "token" not in out["github"]
    assert "ghp_secret" not in str(gcs["github_config"])

"""Settings → GitHub: instance repository mirror configuration and actions.

Called directly by the ``/settings/github`` routes; registry writes reach the
mirror through :class:`back.objects.registry.gitsync.GitHubMirrorHooks`.
"""

from __future__ import annotations

import importlib
import re
import uuid
from typing import Any, Dict

from back.core.errors import InfrastructureError, ValidationError
from shared.config.settings import Settings
from back.core.logging import get_logger
from back.objects.registry.gitsync import GitHubConfig, GitHubSyncService, github_mirror
from back.objects.session import SessionManager
from back.objects.domain.settings.SettingsService import SettingsService

_ss = importlib.import_module("back.objects.domain.settings.SettingsService")

logger = get_logger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class GitHubSettings:
    """Read/save the GitHub mirror settings, test the repository, sync, import."""

    @staticmethod
    def _load(session_mgr: SessionManager, settings: Settings) -> GitHubConfig:
        _, host, token, registry_cfg = SettingsService._resolve_context(
            session_mgr, settings
        )
        return GitHubConfig.from_dict(
            _ss.global_config_service.get_github_config(host, token, registry_cfg)
        )

    @staticmethod
    def _require_ready(cfg: GitHubConfig) -> None:
        if not cfg.owner or not cfg.repo:
            raise ValidationError("Configure the GitHub repository in Settings → GitHub first")

    @staticmethod
    def get_github_result(
        session_mgr: SessionManager, settings: Settings
    ) -> Dict[str, Any]:
        cfg = GitHubSettings._load(session_mgr, settings)
        return {
            "success": True,
            "github": cfg.to_dict(),
            "status": github_mirror.status(),
        }

    @staticmethod
    def save_github_result(
        data: Dict[str, Any], session_mgr: SessionManager, settings: Settings
    ) -> Dict[str, Any]:
        current = GitHubSettings._load(session_mgr, settings)
        incoming = GitHubConfig.from_dict({**current.to_dict(), **(data or {})})
        incoming.instance_uuid = current.instance_uuid or uuid.uuid4().hex
        for label, value in (("owner", incoming.owner), ("repository", incoming.repo)):
            if value and not _NAME_RE.match(value):
                raise ValidationError(f"Invalid GitHub {label} name: {value!r}")
        if incoming.enabled and (not incoming.owner or not incoming.repo):
            raise ValidationError("Owner and repository are required to enable the mirror")
        if incoming.base_path and ".." in incoming.base_path.split("/"):
            raise ValidationError("Base path cannot contain '..'")

        _, host, token, registry_cfg = SettingsService._resolve_context(
            session_mgr, settings
        )
        ok, msg = _ss.global_config_service.set_github_config(
            host, token, registry_cfg, incoming.to_dict()
        )
        if not ok:
            raise InfrastructureError("Failed to save GitHub settings", detail=msg)
        return {"success": True, "github": incoming.to_dict()}

    @staticmethod
    def test_github_result(
        take_over: bool, session_mgr: SessionManager, settings: Settings
    ) -> Dict[str, Any]:
        cfg = GitHubSettings._load(session_mgr, settings)
        GitHubSettings._require_ready(cfg)
        sync = GitHubSyncService(cfg)
        try:
            info = sync.test_connection(take_over=take_over)
        finally:
            sync.close()
        github_mirror.invalidate_config()
        return {"success": True, **info}

    @staticmethod
    def sync_all_github_result(
        actor: str, session_mgr: SessionManager, settings: Settings
    ) -> Dict[str, Any]:
        domain, host, token, registry_cfg = SettingsService._resolve_context(
            session_mgr, settings
        )
        cfg = GitHubSettings._load(session_mgr, settings)
        GitHubSettings._require_ready(cfg)
        svc = _ss.RegistryService.from_context(domain, settings)
        if not svc.cfg.is_configured:
            raise ValidationError("Registry not configured")
        sync = GitHubSyncService(cfg)
        try:
            result = sync.sync_all(
                svc.store,
                actor=actor,
                global_config=_ss.global_config_service.load(host, token, registry_cfg),
            )
        except Exception as exc:
            message = getattr(exc, "message", "") or str(exc)
            github_mirror.record("error", "all domains", "", message, [])
            raise
        finally:
            sync.close()
        sha = result.get("commit_sha") or ""
        github_mirror.record("ok", "all domains", sha, "", result["errors"])
        for item in result.pop("pushed", []):
            if sha or item["errors"]:
                github_mirror.audit(
                    svc.store, cfg, item["folder"], item["version"], actor,
                    "github_synced", commit_sha=sha, errors=item["errors"],
                )
        return {"success": True, **result}

    @staticmethod
    def github_obx_envelope_result(
        actor: str, session_mgr: SessionManager, settings: Settings
    ) -> Dict[str, Any]:
        cfg = GitHubSettings._load(session_mgr, settings)
        GitHubSettings._require_ready(cfg)
        sync = GitHubSyncService(cfg)
        try:
            return sync.build_obx_envelope(exported_by=actor)
        finally:
            sync.close()

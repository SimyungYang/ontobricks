"""Start allow-listed Databricks Jobs as TaskManager background tasks."""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from back.core.databricks.jobs import (
    JOB_KIND,
    TASK_TYPE,
    JobActionRunner,
    job_action_full_name,
    parse_job_action_id,
)
from back.core.databricks.request_identity import get_request_identity
from back.core.errors import ConflictError, ValidationError
from back.core.helpers import run_blocking
from back.core.logging import get_logger
from back.core.task_manager import get_task_manager
from back.objects.digitaltwin.DigitalTwin import DigitalTwin

logger = get_logger(__name__)


def _poll_job_task(
    tm: Any,
    runner: JobActionRunner,
    task_id: str,
    job_id: int,
    local_id: str,
    run_id: int,
    full_name: str,
    entity_uri: str,
) -> None:
    """Wait for a Databricks run and complete or fail the TaskManager task."""
    try:
        outcome = runner.wait_for(
            run_id,
            on_progress=lambda pct, msg: tm.update_progress(task_id, pct, msg),
        )
        payload = {
            "kind": JOB_KIND,
            "job_id": job_id,
            "entity_id": local_id,
            "run_id": run_id,
            "run_page_url": outcome.get("run_page_url"),
            "result_state": outcome.get("result_state"),
            "output": outcome.get("output"),
        }
        if outcome.get("success"):
            tm.complete_task(
                task_id,
                result=payload,
                message=outcome.get("message") or "Job completed",
            )
            return
        tm.fail_task(
            task_id,
            outcome.get("message") or outcome.get("result_state") or "Job failed",
        )
        running = tm.get_task(task_id)
        if running is not None:
            running.result = payload
    except Exception as exc:  # noqa: BLE001
        logger.warning("entity job %s for %s failed: %s", full_name, entity_uri, exc)
        tm.fail_task(task_id, str(exc) or "Job failed")


class JobActionService:
    """Invoke a class-declared Job and track it in Background Tasks."""

    @staticmethod
    async def invoke(
        domain: Any,
        settings: Any,
        *,
        entity_uri: str,
        action: Dict[str, Any],
        matched_cls: Dict[str, Any],
    ) -> Dict[str, Any]:
        """``run_now`` then poll in a worker. Returns ``task_id`` immediately."""
        job_id = parse_job_action_id(action.get("job_id") or action.get("fullName"))
        if job_id is None:
            raise ValidationError("Job action is missing a valid job_id")
        if action.get("notebook_path"):
            raise ValidationError("Notebook paths cannot be invoked as class actions")

        local_id = DigitalTwin.extract_local_id(entity_uri)
        class_name = matched_cls.get("name", "")
        dname = domain.domain_folder or (domain.info or {}).get("name", "")
        job_name = str(action.get("job_name") or action.get("function") or job_id)
        full_name = job_action_full_name(job_id)

        tm = get_task_manager()
        for task in tm.get_active_tasks():
            if task.task_type != TASK_TYPE:
                continue
            meta = task.result if isinstance(task.result, dict) else {}
            if meta.get("job_id") == job_id and meta.get("entity_id") == local_id:
                raise ConflictError(
                    f"Job {job_name} is already running for {local_id}"
                )

        token = get_request_identity().user_token
        runner = JobActionRunner.from_domain(domain, settings, user_token=token)
        run_id = await run_blocking(
            runner.run_now,
            job_id,
            entity_id=local_id,
            entity_uri=entity_uri,
            class_name=class_name,
            domain=dname,
        )

        task = tm.create_task(
            name=f"Job: {job_name} on {local_id}",
            task_type=TASK_TYPE,
            steps=[
                {"name": "start", "description": "Starting Databricks job"},
                {"name": "run", "description": "Waiting for the job run"},
            ],
        )
        task.result = {
            "job_id": job_id,
            "entity_id": local_id,
            "run_id": run_id,
            "kind": JOB_KIND,
        }
        tm.start_task(task.id, f"Started Databricks run {run_id}")
        threading.Thread(
            target=_poll_job_task,
            args=(tm, runner, task.id, job_id, local_id, run_id, full_name, entity_uri),
            daemon=True,
        ).start()
        logger.info(
            "nodes/action: entity=%s class=%s domain=%s job=%s task=%s run=%s",
            local_id,
            class_name,
            dname,
            job_id,
            task.id,
            run_id,
        )
        return {
            "success": True,
            "kind": JOB_KIND,
            "entity_uri": entity_uri,
            "entity_local_id": local_id,
            "class_name": class_name,
            "action": full_name,
            "task_id": task.id,
            "run_id": run_id,
            "returns_table": False,
            "rows": None,
            "message": "Running in Background Tasks",
        }

"""Unit tests for the synced-table REST clients behind :class:`SyncedTableManager`.

Lakebase Autoscaling projects expose synced tables under
``/api/2.0/postgres/synced_tables``; the legacy ``/api/2.0/database``
endpoints only know database *instances*. The routed client creates through
the Postgres API and keeps reading/deleting tables registered either way.
"""

from __future__ import annotations

from typing import Any

import pytest

from back.core.graphdb.lakebase.SyncedTableManager import (
    SyncedTableManager,
    _CompatWorkspaceClient,
)

FQN = "cat.graph_schema.domain_v1_sync"

_PG_TABLE = {
    "name": f"synced_tables/{FQN}",
    "synced_table_id": FQN,
    "spec": {"branch": "projects/onto/branches/production"},
    "status": {
        "pipeline_id": "pipe-1",
        "detailed_state": "SYNCED_TABLE_PROVISIONING_PIPELINE_RESOURCES",
        "message": "pending setup",
    },
}


class _FakeApiClient:
    """Records calls and answers from a ``{(method, path): response}`` table."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def do(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path, kwargs))
        answer = self.routes.get((method, path))
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise ValueError(f"404 Not Found: {path}")
        return answer


def _database(routes: dict[tuple[str, str], Any]) -> tuple[Any, _FakeApiClient]:
    api = _FakeApiClient(routes)
    client = _CompatWorkspaceClient(type("WC", (), {"api_client": api})())
    return client.database, api


def _legacy_payload() -> dict[str, Any]:
    return SyncedTableManager(
        project_name="onto", branch_name="production", logical_db="databricks_postgres"
    )._build_synced_table_dict(
        name=FQN,
        source_table_full_name="cat.src.triplestore_v1",
        primary_key_columns=["subject", "predicate", "object_hash"],
        sync_mode="snapshot",
        create_database_objects_if_missing=True,
    )


@pytest.mark.unit
class TestCreate:
    def test_creates_through_postgres_api_with_branch_path(self):
        db, api = _database({
            ("POST", "/api/2.0/postgres/synced_tables"): {"done": True, "response": _PG_TABLE},
        })
        db.create_synced_database_table(synced_table=_legacy_payload())
        method, path, kwargs = api.calls[0]
        assert (method, path) == ("POST", "/api/2.0/postgres/synced_tables")
        assert kwargs["query"] == {"synced_table_id": FQN}
        assert kwargs["body"]["spec"] == {
            "source_table_full_name": "cat.src.triplestore_v1",
            "primary_key_columns": ["subject", "predicate", "object_hash"],
            "scheduling_policy": "SNAPSHOT",
            "create_database_objects_if_missing": True,
            "branch": "projects/onto/branches/production",
            "postgres_database": "databricks_postgres",
        }

    def test_create_returns_status_in_legacy_shape(self):
        db, _ = _database({
            ("POST", "/api/2.0/postgres/synced_tables"): {"done": True, "response": _PG_TABLE},
        })
        synced = db.create_synced_database_table(synced_table=_legacy_payload())
        assert SyncedTableManager._extract_pipeline_id(synced) == "pipe-1"
        assert SyncedTableManager._extract_state(synced) == "PROVISIONING_PIPELINE_RESOURCES"
        assert synced.name == FQN

    def test_falls_back_to_database_api_when_postgres_project_is_unknown(self):
        db, api = _database({
            ("POST", "/api/2.0/postgres/synced_tables"): ValueError("404 Not Found: project"),
            ("POST", "/api/2.0/database/synced_tables"): {"name": FQN},
        })
        db.create_synced_database_table(synced_table=_legacy_payload())
        assert [c[1] for c in api.calls] == [
            "/api/2.0/postgres/synced_tables",
            "/api/2.0/database/synced_tables",
        ]

    def test_instance_without_branch_uses_database_api_directly(self):
        db, api = _database({("POST", "/api/2.0/database/synced_tables"): {"name": FQN}})
        payload = SyncedTableManager(
            project_name="legacy-instance", logical_db="databricks_postgres"
        )._build_synced_table_dict(
            name=FQN,
            source_table_full_name="cat.src.triplestore_v1",
            primary_key_columns=["subject", "predicate", "object_hash"],
            sync_mode="snapshot",
            create_database_objects_if_missing=True,
        )
        db.create_synced_database_table(synced_table=payload)
        assert [c[1] for c in api.calls] == ["/api/2.0/database/synced_tables"]

    def test_already_exists_from_postgres_api_is_not_retried_on_database_api(self):
        db, api = _database({
            ("POST", "/api/2.0/postgres/synced_tables"): ValueError("Synced table already exists"),
        })
        with pytest.raises(ValueError, match="already exists"):
            db.create_synced_database_table(synced_table=_legacy_payload())
        assert len(api.calls) == 1


@pytest.mark.unit
class TestGetAndDelete:
    def test_get_reads_postgres_api_first(self):
        db, api = _database({("GET", f"/api/2.0/postgres/synced_tables/{FQN}"): _PG_TABLE})
        synced = db.get_synced_database_table(name=FQN)
        assert SyncedTableManager._extract_pipeline_id(synced) == "pipe-1"
        assert len(api.calls) == 1

    def test_get_finds_table_registered_through_database_api(self):
        legacy = {"name": FQN, "data_synchronization_status": {"pipeline_id": "old-pipe"}}
        db, _ = _database({("GET", f"/api/2.0/database/synced_tables/{FQN}"): legacy})
        synced = db.get_synced_database_table(name=FQN)
        assert SyncedTableManager._extract_pipeline_id(synced) == "old-pipe"

    def test_get_not_found_on_both_apis_raises_not_found(self):
        db, _ = _database({})
        with pytest.raises(ValueError, match="not found"):
            db.get_synced_database_table(name=FQN)

    def test_delete_removes_from_both_apis(self):
        db, api = _database({
            ("DELETE", f"/api/2.0/postgres/synced_tables/{FQN}"): {"done": True},
            ("DELETE", f"/api/2.0/database/synced_tables/{FQN}"): {},
        })
        db.delete_synced_database_table(name=FQN)
        assert sorted(c[1] for c in api.calls) == [
            f"/api/2.0/database/synced_tables/{FQN}",
            f"/api/2.0/postgres/synced_tables/{FQN}",
        ]

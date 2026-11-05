"""Build + metadata-refresh hooks run the definition sync once and surface the report."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import back.objects.domain.Domain as _  # noqa: F401  (module lookup below)
from back.objects.digitaltwin.build.TwinGraphBuild import TwinGraphBuild
from back.objects.domain import Domain, _metadata_tasks
from back.objects.ontology.definitions import DefinitionReferences, DefinitionSyncService
from tests.units.definitions.conftest import (
    SALES_URI,
    FakeMetricSource,
    build_mapping,
    build_metadata,
    build_ontology,
)

pytestmark = pytest.mark.unit

domain_module = sys.modules["back.objects.domain.Domain"]
REPORT = {"message": "1 definitions updated, 0 warnings", "summary": {"updated": 1}}


class _Client(FakeMetricSource):
    """Metadata-refresh client: table reads plus the metric definition."""

    def get_metric_view_columns_with_roles(self, catalog, schema, name):
        return build_metadata()["tables"][0]["columns"]

    def get_table_columns(self, catalog, schema, name):
        return []

    def get_table_comment(self, catalog, schema, name):
        return ""

    def check_table_select_permission(self, catalog, schema, name, **kwargs):
        return {"can_select": True, "error": ""}


# ---------------------------------------------------------------------------
# Knowledge-graph build
# ---------------------------------------------------------------------------


def _build_domain():
    order = []
    domain = SimpleNamespace(
        info={"name": "Sales"},
        databricks={},
        delta={},
        triplestore={},
        settings={},
        current_version="1",
        ontology={"base_uri": "http://test.org/onto#"},
        generated={},
        assignment={"entities": []},
        last_update="",
        last_build="",
        save=MagicMock(),
        ensure_generated_content=MagicMock(side_effect=lambda: order.append("owl")),
        get_r2rml=MagicMock(return_value="@prefix rr: <http://www.w3.org/ns/r2rml#> ."),
    )
    return domain, order


def _prepare(domain):
    with patch.object(TwinGraphBuild, "pipeline_steps", return_value=[{"name": "graph"}]):
        return TwinGraphBuild.prepare_session_sync(
            domain,
            MagicMock(),
            view_table_fn=lambda d: "c.s.v",
            graph_name_fn=lambda d: "g",
            get_credentials=lambda d, s: ("https://h", "tok", "wh"),
            is_databricks_app_fn=lambda: False,
        )


class TestBuildHook:
    def test_build_syncs_exactly_once_before_owl_generation(self):
        domain, order = _build_domain()
        with patch.object(
            DefinitionSyncService,
            "sync_session",
            side_effect=lambda d, f: order.append("sync") or REPORT,
        ) as sync:
            _prepare(domain)
        assert (sync.call_count, order) == (1, ["sync", "owl"])

    def test_build_result_carries_the_drift_report(self):
        from back.objects.digitaltwin.build._build_pipeline import _BuildPipeline

        pipe = object.__new__(_BuildPipeline)
        ontology = {}
        DefinitionReferences.set_last_report(ontology, REPORT)
        pipe.__dict__.update(
            tm=MagicMock(), task_id="t", start_time=0.0, phase_times={}, is_api=False,
            build_kind="session", domain_name="Sales", triple_count=3, view_table="c.s.v",
            graph_name="g", ontology_config=ontology,
        )
        pipe._record_build_run = MagicMock()
        pipe._persist_last_build_to_registry = MagicMock()
        pipe._complete_task()
        kwargs = pipe.tm.complete_task.call_args.kwargs
        assert (kwargs["result"]["definition_sync"], kwargs["message"].endswith(REPORT["message"])) == (
            REPORT,
            True,
        )


# ---------------------------------------------------------------------------
# Metadata refresh (sync endpoint)
# ---------------------------------------------------------------------------


def _sync_path_domain(monkeypatch, domain_session):
    monkeypatch.setattr(domain_module, "get_databricks_host_and_token", lambda s, st: ("h", "t"))
    monkeypatch.setattr(domain_module, "resolve_warehouse_id", lambda s, st: "wh")
    monkeypatch.setattr(domain_module, "DatabricksClient", lambda **kw: _Client())
    domain_session._data["ontology"].update(build_ontology())
    domain_session.catalog_metadata.update(build_metadata())
    domain_session.assignment["entities"] = [build_mapping()]
    return Domain(domain_session, MagicMock())


class TestMetadataRefreshHook:
    def test_sync_refresh_runs_definition_sync_once(self, monkeypatch, domain_session):
        domain = _sync_path_domain(monkeypatch, domain_session)
        with patch.object(DefinitionSyncService, "sync_session", return_value=REPORT) as sync:
            result = domain.update_metadata_tables(None)
        assert (sync.call_count, result["definition_sync"]) == (1, REPORT)

    def test_sync_refresh_pulls_the_metric_definition(self, monkeypatch, domain_session, metric_defs):
        domain = _sync_path_domain(monkeypatch, domain_session)
        monkeypatch.setattr(domain_module, "DatabricksClient", lambda **kw: _Client(metric_defs))
        result = domain.update_metadata_tables(None)
        assert result["definition_sync"]["summary"]["updated"] == 2


# ---------------------------------------------------------------------------
# Metadata refresh (async task + Apply)
# ---------------------------------------------------------------------------


def _run_async(monkeypatch, metric_defs, snapshot):
    tm = MagicMock()
    monkeypatch.setattr(_metadata_tasks, "get_task_manager", lambda: tm)
    monkeypatch.setattr(_metadata_tasks, "DatabricksClient", lambda **kw: _Client(metric_defs))
    metadata = build_metadata()
    tables = {t["name"]: t for t in metadata["tables"]}
    _metadata_tasks.run_metadata_update_task(
        "task-1", "h", "t", "wh", "main", "sales", list(tables), metadata, tables,
        snapshot, [build_mapping()] if snapshot is not None else None,
    )
    return tm.complete_task.call_args.kwargs


class TestAsyncMetadataRefreshHook:
    def test_task_syncs_once_and_reports(self, monkeypatch, metric_defs):
        with patch.object(
            DefinitionSyncService, "sync", autospec=True, return_value=(REPORT, True)
        ) as sync:
            kwargs = _run_async(monkeypatch, metric_defs, build_ontology())
        assert (sync.call_count, kwargs["result"]["definition_sync"]) == (1, REPORT)

    def test_task_message_includes_the_drift_summary(self, monkeypatch, metric_defs):
        kwargs = _run_async(monkeypatch, metric_defs, build_ontology())
        assert kwargs["message"].endswith("2 definitions updated, 0 warnings")

    def test_task_without_snapshot_skips_the_sync(self, monkeypatch, metric_defs):
        with patch.object(DefinitionSyncService, "sync", autospec=True) as sync:
            kwargs = _run_async(monkeypatch, metric_defs, None)
        assert (sync.call_count, kwargs["result"]["definition_sync"]) == (0, {})

    def test_apply_adopts_the_synced_definitions(self, monkeypatch, metric_defs, domain_session):
        result = _run_async(monkeypatch, metric_defs, build_ontology())["result"]
        domain_session._data["ontology"].update(build_ontology())
        Domain(domain_session).save_metadata_tables(
            result["metadata"]["tables"], definition_refs=result["definition_refs"]
        )
        item = DefinitionReferences.items(domain_session.ontology)[f"{SALES_URI}|totalRevenue"]
        assert DefinitionReferences.effective(item)["comment"] == "Gross revenue in EUR"

    def test_apply_never_creates_glossary_refs_from_the_payload(self, domain_session):
        domain_session._data["ontology"].update(build_ontology())
        incoming = {"items": {SALES_URI: {"class_uri": SALES_URI, "attribute": "", "glossary_ref": "x"}}}
        DefinitionReferences.adopt_synced(domain_session.ontology, incoming)
        assert SALES_URI not in DefinitionReferences.items(domain_session.ontology)

"""DefinitionSyncService: UC content -> ontology, plus the drift report."""

import copy
import importlib
from unittest.mock import patch

import pytest

from back.core.databricks.uc import GlossaryConnector, NotAvailableGlossaryConnector
from back.core.errors import InfrastructureError
from back.objects.ontology.definitions import DefinitionReferences, DefinitionSyncService
from tests.units.definitions.conftest import (
    SALES_URI,
    FakeMetricSource,
    build_mapping,
    build_metadata,
    build_ontology,
)

pytestmark = pytest.mark.unit

_not_available_module = importlib.import_module(
    "back.core.databricks.uc.NotAvailableGlossaryConnector"
)

REVENUE_KEY = f"{SALES_URI}|totalRevenue"
T1, T2 = "2026-11-05T10:00:00+00:00", "2026-11-05T11:00:00+00:00"


class _Glossary(GlossaryConnector):
    name = "fake_pages"

    def __init__(self, terms):
        self.terms = terms

    def is_available(self):
        return True

    def fetch_terms(self, refs):
        return {ref: self.terms.get(ref) for ref in refs}


def _sync(ontology, source, *, clock=T1, glossary=None, mappings=None):
    service = DefinitionSyncService(source, glossary=glossary, clock=lambda: clock)
    mappings = [build_mapping()] if mappings is None else mappings
    return service.sync(ontology, mappings, build_metadata())


def _entry(report, key=REVENUE_KEY):
    return next(e for e in report["entries"] if e["key"] == key)


class TestMetricViewSync:
    def test_first_sync_updates_the_attribute(self, metric_defs):
        ontology = build_ontology()
        report, changed = _sync(ontology, FakeMetricSource(metric_defs))
        values = DefinitionReferences.effective(DefinitionReferences.items(ontology)[REVENUE_KEY])
        assert (changed, values["comment"], values["alt_labels"]) == (
            True,
            "Gross revenue in EUR",
            ["revenue", "turnover"],
        )

    def test_changed_expression_is_reported_before_and_after(self, metric_defs):
        ontology = build_ontology()
        _sync(ontology, FakeMetricSource(metric_defs))
        changed_defs = copy.deepcopy(metric_defs)
        changed_defs["total_revenue"]["expr"] = "SUM(amount - discount)"
        report, _ = _sync(ontology, FakeMetricSource(changed_defs), clock=T2)
        assert _entry(report)["changes"]["expr"] == {
            "before": "SUM(amount)",
            "after": "SUM(amount - discount)",
        }

    def test_new_synonym_is_reported(self, metric_defs):
        ontology = build_ontology()
        _sync(ontology, FakeMetricSource(metric_defs))
        changed_defs = copy.deepcopy(metric_defs)
        changed_defs["total_revenue"]["synonyms"].append("sales")
        report, _ = _sync(ontology, FakeMetricSource(changed_defs), clock=T2)
        assert _entry(report)["changes"]["synonyms_added"] == ["sales"]

    def test_unchanged_definition_is_a_no_op(self, metric_defs):
        ontology = build_ontology()
        _sync(ontology, FakeMetricSource(metric_defs))
        before = copy.deepcopy(DefinitionReferences.items(ontology))
        report, changed = _sync(ontology, FakeMetricSource(metric_defs), clock=T2)
        assert (changed, _entry(report)["status"], DefinitionReferences.items(ontology)) == (
            False,
            "unchanged",
            before,
        )

    def test_missing_target_keeps_last_value_and_warns(self, metric_defs):
        ontology = build_ontology()
        _sync(ontology, FakeMetricSource(metric_defs))
        gone = {k: v for k, v in metric_defs.items() if k != "total_revenue"}
        report, _ = _sync(ontology, FakeMetricSource(gone), clock=T2)
        item = DefinitionReferences.items(ontology)[REVENUE_KEY]
        assert (
            _entry(report)["warnings"][0]["reason"],
            DefinitionReferences.effective(item)["comment"],
            "no longer exists" in item["warning"],
        ) == ("target_missing", "Gross revenue in EUR", True)

    def test_unreadable_view_keeps_last_value_and_warns(self, metric_defs):
        ontology = build_ontology()
        _sync(ontology, FakeMetricSource(metric_defs))
        report, _ = _sync(
            ontology, FakeMetricSource(error=InfrastructureError("view dropped")), clock=T2
        )
        item = DefinitionReferences.items(ontology)[REVENUE_KEY]
        assert (_entry(report)["status"], item["synced"]["metric_view"]["expr"]) == (
            "warning",
            "SUM(amount)",
        )

    def test_new_warning_counts_as_a_change(self, metric_defs):
        ontology = build_ontology()
        _sync(ontology, FakeMetricSource(metric_defs))
        _, changed = _sync(
            ontology, FakeMetricSource(error=InfrastructureError("view dropped")), clock=T2
        )
        assert changed is True

    def test_repeated_warning_is_a_no_op(self, metric_defs):
        ontology = build_ontology()
        broken = FakeMetricSource(error=InfrastructureError("view dropped"))
        _sync(ontology, FakeMetricSource(metric_defs))
        _sync(ontology, broken, clock=T2)
        _, changed = _sync(ontology, broken, clock=T2)
        assert changed is False

    def test_warning_clears_once_the_target_is_back(self, metric_defs):
        ontology = build_ontology()
        _sync(ontology, FakeMetricSource({}))
        _sync(ontology, FakeMetricSource(metric_defs), clock=T2)
        assert "warning" not in DefinitionReferences.items(ontology)[REVENUE_KEY]

    def test_non_referenced_attribute_is_untouched(self, metric_defs):
        ontology = build_ontology()
        _sync(ontology, FakeMetricSource(metric_defs))
        assert f"{SALES_URI}|note" not in DefinitionReferences.items(ontology)

    def test_report_summarises_updates_and_warnings(self, metric_defs):
        ontology = build_ontology()
        report, _ = _sync(ontology, FakeMetricSource(metric_defs))
        assert report["message"] == "2 definitions updated, 0 warnings"

    def test_each_metric_view_is_read_once(self, metric_defs):
        source = FakeMetricSource(metric_defs)
        _sync(build_ontology(), source)
        assert source.calls == [("main", "sales", "revenue_mv")]

    def test_no_reference_means_no_report_and_no_io(self):
        source = FakeMetricSource({})
        report, changed = _sync(build_ontology(), source, mappings=[])
        assert (report, changed, source.calls) == ({}, False, [])


class TestGlossarySync:
    def test_unavailable_pages_api_logs_info_and_does_not_fail(self):
        ontology = build_ontology()
        DefinitionReferences.set_glossary_ref(ontology, SALES_URI, "", "page-7")
        with patch.object(_not_available_module.logger, "info") as info:
            report, _ = _sync(ontology, FakeMetricSource({}), mappings=[])
        assert (
            _entry(report, SALES_URI)["warnings"][0]["reason"],
            report["glossary_connector"],
            "UC Pages API not available" in info.call_args.args[0],
        ) == ("source_unavailable", {"name": "not_available", "available": False}, True)

    def test_unavailable_pages_api_keeps_user_text_of_never_synced_class(self):
        ontology = build_ontology()
        DefinitionReferences.set_glossary_ref(ontology, SALES_URI, "", "page-7")
        _sync(ontology, FakeMetricSource({}), mappings=[])
        assert ontology["classes"][0]["description"] == "User-written description"

    def test_page_definition_overwrites_class_comment_and_alt_labels(self):
        ontology = build_ontology()
        DefinitionReferences.set_glossary_ref(ontology, SALES_URI, "", "page-7")
        glossary = _Glossary({"page-7": {"definition": "A closed deal", "synonyms": ["order"]}})
        _sync(ontology, FakeMetricSource({}), glossary=glossary, mappings=[])
        cls = ontology["classes"][0]
        assert (cls["description"], cls["alternate_labels"]) == ("A closed deal", ["order"])

    def test_deleted_page_keeps_last_definition_with_warning(self):
        ontology = build_ontology()
        DefinitionReferences.set_glossary_ref(ontology, SALES_URI, "", "page-7")
        term = {"definition": "A closed deal", "synonyms": []}
        _sync(ontology, FakeMetricSource({}), glossary=_Glossary({"page-7": term}), mappings=[])
        report, _ = _sync(
            ontology, FakeMetricSource({}), glossary=_Glossary({}), mappings=[], clock=T2
        )
        assert (ontology["classes"][0]["description"], _entry(report, SALES_URI)["status"]) == (
            "A closed deal",
            "warning",
        )

    def test_page_definition_wins_over_metric_comment(self, metric_defs):
        ontology = build_ontology()
        DefinitionReferences.set_glossary_ref(ontology, SALES_URI, "totalRevenue", "page-9")
        glossary = _Glossary({"page-9": {"definition": "Revenue (finance)", "synonyms": ["rev"]}})
        _sync(ontology, FakeMetricSource(metric_defs), glossary=glossary)
        values = DefinitionReferences.effective(DefinitionReferences.items(ontology)[REVENUE_KEY])
        assert (values["comment"], values["alt_labels"]) == (
            "Revenue (finance)",
            ["revenue", "turnover", "rev"],
        )

    def test_not_available_connector_never_fetches(self):
        assert NotAvailableGlossaryConnector().fetch_terms(["x"]) == {}


class TestSessionEntryPoint:
    def test_sync_session_invalidates_generated_owl_on_change(self, metric_defs, domain_session):
        domain_session._data["ontology"].update(build_ontology())
        domain_session.catalog_metadata.update(build_metadata())
        domain_session.assignment["entities"] = [build_mapping()]
        domain_session.generated["owl"] = "stale"
        DefinitionSyncService.sync_session(domain_session, lambda: FakeMetricSource(metric_defs))
        assert domain_session.generated["owl"] == ""

    def test_sync_session_builds_no_client_without_references(self, domain_session):
        def _factory():
            raise AssertionError("client must not be built")

        domain_session._data["ontology"].update(build_ontology())
        assert DefinitionSyncService.sync_session(domain_session, _factory) == {}

"""derivedFrom is set from mappings that bind attributes to metric-view columns."""

import copy

import pytest

from back.objects.mapping import Mapping, MetricViewBindings
from back.objects.ontology.definitions import DefinitionReferences
from tests.units.definitions.conftest import (
    MV,
    SALES_URI,
    build_mapping,
    build_metadata,
    build_ontology,
)

pytestmark = pytest.mark.unit

REVENUE_KEY = f"{SALES_URI}|totalRevenue"


def _session(domain_session):
    domain_session._data["ontology"].update(build_ontology())
    domain_session.catalog_metadata.update(build_metadata())
    return domain_session


class TestResolve:
    def test_measure_binding_is_resolved_with_its_role(self):
        bindings = MetricViewBindings.resolve([build_mapping()], build_metadata())
        assert bindings[REVENUE_KEY] == {"view": MV, "column": "total_revenue", "role": "measure"}

    def test_plain_table_source_is_not_a_binding(self):
        assert MetricViewBindings.resolve([build_mapping()], build_metadata("table")) == {}

    def test_excluded_attribute_is_not_bound(self):
        mapping = build_mapping(excluded_attributes=["totalRevenue"])
        assert REVENUE_KEY not in MetricViewBindings.resolve([mapping], build_metadata())

    def test_binding_survives_a_view_missing_from_metadata(self):
        previous = {REVENUE_KEY: {"view": MV, "column": "total_revenue", "role": "measure"}}
        bindings = MetricViewBindings.resolve([build_mapping()], {"tables": []}, previous)
        assert bindings[REVENUE_KEY]["view"] == MV


class TestMappingSavePath:
    def test_binding_an_attribute_sets_derived_from(self, domain_session):
        session = _session(domain_session)
        Mapping(session).add_or_update_entity_mapping(build_mapping())
        session.ensure_generated_content()
        item = DefinitionReferences.items(session.ontology)[REVENUE_KEY]
        assert item["derived_from"]["column"] == "total_revenue"

    def test_derived_from_lands_in_the_generated_owl(self, domain_session):
        session = _session(domain_session)
        Mapping(session).add_or_update_entity_mapping(build_mapping())
        session.ensure_generated_content()
        assert f'"{MV}#total_revenue"' in session.generated["owl"]

    def test_existing_mapping_is_left_untouched(self, domain_session):
        session = _session(domain_session)
        Mapping(session).add_or_update_entity_mapping(build_mapping())
        before = copy.deepcopy(session.get_entity_mappings())
        session.ensure_generated_content()
        assert session.get_entity_mappings() == before

    def test_unbinding_clears_derived_from(self, domain_session):
        session = _session(domain_session)
        mapper = Mapping(session)
        mapper.add_or_update_entity_mapping(build_mapping())
        session.ensure_generated_content()
        mapper.add_or_update_entity_mapping(build_mapping(attribute_mappings={"region": "region"}))
        session.ensure_generated_content()
        assert REVENUE_KEY not in DefinitionReferences.items(session.ontology)

    def test_deleting_the_mapping_clears_derived_from(self, domain_session):
        session = _session(domain_session)
        mapper = Mapping(session)
        mapper.add_or_update_entity_mapping(build_mapping())
        session.ensure_generated_content()
        mapper.delete_entity_mapping(SALES_URI)
        session.ensure_generated_content()
        assert not DefinitionReferences.has_items(session.ontology)

    def test_glossary_reference_survives_unbinding(self, domain_session):
        session = _session(domain_session)
        mapper = Mapping(session)
        mapper.add_or_update_entity_mapping(build_mapping())
        session.ensure_generated_content()
        DefinitionReferences.set_glossary_ref(session.ontology, SALES_URI, "totalRevenue", "page-42")
        mapper.delete_entity_mapping(SALES_URI)
        session.ensure_generated_content()
        item = DefinitionReferences.items(session.ontology)[REVENUE_KEY]
        assert ("derived_from" in item, item["glossary_ref"]) == (False, "page-42")

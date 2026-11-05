"""OWL output of UC definition references (annotation properties + synced text)."""

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from back.objects.ontology import Owl
from back.objects.ontology.definitions import DefinitionReferences, DefinitionSyncService
from shared.config.constants import ONTOBRICKS_NS
from tests.units.definitions.conftest import (
    BASE,
    SALES_URI,
    FakeMetricSource,
    build_mapping,
    build_metadata,
    build_ontology,
)

pytestmark = pytest.mark.unit

REVENUE = URIRef(BASE + "totalRevenue")
SALES = URIRef(SALES_URI)


@pytest.fixture
def graph(metric_defs):
    ontology = build_ontology()
    DefinitionReferences.set_glossary_ref(ontology, SALES_URI, "", "https://pages.example/t/7")
    DefinitionSyncService(FakeMetricSource(metric_defs), clock=lambda: "2026-11-05T10:00:00+00:00").sync(
        ontology, [build_mapping()], build_metadata()
    )
    g = Graph()
    g.parse(data=Owl.generate_owl(ontology), format="turtle")
    return g


def test_annotation_properties_are_declared_once(graph):
    declared = list(graph.subjects(RDF.type, OWL.AnnotationProperty))
    assert declared.count(ONTOBRICKS_NS.derivedFrom) == 1


def test_all_definition_properties_are_declared(graph):
    declared = set(graph.subjects(RDF.type, OWL.AnnotationProperty))
    assert {ONTOBRICKS_NS.derivedFrom, ONTOBRICKS_NS.glossaryRef, ONTOBRICKS_NS.syncedAt} <= declared


def test_derived_from_points_at_the_metric_view_column(graph):
    assert graph.value(REVENUE, ONTOBRICKS_NS.derivedFrom) == Literal(
        "main.sales.revenue_mv#total_revenue"
    )


def test_attribute_gets_uc_comment(graph):
    assert graph.value(REVENUE, RDFS.comment) == Literal("Gross revenue in EUR")


def test_attribute_gets_uc_synonyms_as_alt_labels(graph):
    assert set(graph.objects(REVENUE, SKOS.altLabel)) == {Literal("revenue"), Literal("turnover")}


def test_synced_at_and_source_are_recorded(graph):
    assert (
        str(graph.value(REVENUE, ONTOBRICKS_NS.syncedAt)),
        str(graph.value(REVENUE, ONTOBRICKS_NS.definitionSource)),
    ) == ("2026-11-05T10:00:00+00:00", "uc_metric_view:main.sales.revenue_mv")


def test_glossary_ref_and_see_also_on_the_class(graph):
    ref = "https://pages.example/t/7"
    assert (graph.value(SALES, ONTOBRICKS_NS.glossaryRef), graph.value(SALES, RDFS.seeAlso)) == (
        Literal(ref),
        URIRef(ref),
    )


def test_unsynced_glossary_class_keeps_user_comment(graph):
    assert graph.value(SALES, RDFS.comment) == Literal("User-written description")


def test_non_referenced_attribute_has_no_definition_annotations(graph):
    assert graph.value(URIRef(BASE + "note"), ONTOBRICKS_NS.derivedFrom) is None


def test_no_references_means_no_declarations():
    g = Graph()
    g.parse(data=Owl.generate_owl(build_ontology()), format="turtle")
    assert (ONTOBRICKS_NS.derivedFrom, RDF.type, OWL.AnnotationProperty) not in g

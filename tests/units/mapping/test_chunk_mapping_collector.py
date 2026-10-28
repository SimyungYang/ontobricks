"""Auto-assign keeps chunk mappings even when the agent mistypes the URI host."""

import pytest

from back.objects.mapping import ChunkMappingCollector

pytestmark = pytest.mark.unit

BASE = "https://databricks-ontology.com/LGChemCathodeTrace#"
CHUNK = {f"{BASE}judgedAtProcess", f"{BASE}judgesLot"}


def _rel(uri):
    return {"property": uri, "sql_query": "SELECT 1 AS source_id, 2 AS target_id"}


def test_exact_uri_is_kept():
    into = {}
    ChunkMappingCollector.collect([_rel(f"{BASE}judgesLot")], ("property",), CHUNK, into)
    assert list(into) == [f"{BASE}judgesLot"]


def test_mistyped_host_is_repaired_to_the_chunk_uri():
    into = {}
    wrong = "https://databricks-ontobricks.com/LGChemCathodeTrace#judgedAtProcess"
    ChunkMappingCollector.collect([_rel(wrong)], ("property",), CHUNK, into)
    assert into[f"{BASE}judgedAtProcess"]["property"] == f"{BASE}judgedAtProcess"


def test_mapping_outside_the_chunk_is_dropped():
    into = {}
    kept = ChunkMappingCollector.collect([_rel(f"{BASE}parentLot")], ("property",), CHUNK, into)
    assert (kept, into) == (0, {})


def test_ambiguous_local_name_is_dropped():
    into = {}
    chunk = {f"{BASE}judgesLot", "https://other.example/Onto#judgesLot"}
    ChunkMappingCollector.collect([_rel("https://typo.example/X#judgesLot")], ("property",), chunk, into)
    assert into == {}


def test_entity_mapping_rewrites_every_present_uri_key():
    into = {}
    uri = f"{BASE}ProcessInspection"
    wrong = {"ontology_class": "https://databricks-ontobricks.com/LGChemCathodeTrace#ProcessInspection",
             "sql_query": "SELECT 1"}
    ChunkMappingCollector.collect([wrong], ("ontology_class", "class_uri"), {uri}, into)
    assert into[uri]["ontology_class"] == uri and "class_uri" not in into[uri]

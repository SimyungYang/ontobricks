"""Regression tests for agents/tools/mapping.py's SQL deduplication guard.

Real-world bug: the auto-mapping agent's own prompt instructs it to alias
the id/label source column (e.g. "AS Label") *and* repeat it under its
original name for attribute mapping. When that original name is itself
"id"/"label" (case-insensitive) — e.g. a `gold_segment` table with an
actual column named `label` — this produces two columns Databricks
resolves as the same one, and the digital-twin build fails with
`AMBIGUOUS_REFERENCE` at Sync time. `tool_submit_entity_mapping` /
`tool_submit_relationship_mapping` must deduplicate before persisting the
mapping, regardless of what the LLM actually generated.
"""

import pytest

from agents.tools.context import ToolContext
from agents.tools.mapping import (
    tool_submit_entity_mapping,
    tool_submit_relationship_mapping,
)


@pytest.fixture
def ctx():
    return ToolContext(host="https://test.databricks.com", token="fake-token")


@pytest.mark.unit
class TestEntityMappingLabelCollision:
    def test_label_column_named_label_is_deduplicated(self, ctx):
        """The exact production shape: label_column="Label" sourced from a
        physical column literally named "label", also listed as a
        standalone attribute — the classic self-collision."""
        sql = (
            "SELECT segment_id AS ID, label AS Label, segment_id, label, description "
            "FROM catalog.schema.gold_segment WHERE segment_id IS NOT NULL"
        )
        tool_submit_entity_mapping(
            ctx,
            class_uri="http://test.org/ontology#Segment",
            class_name="Segment",
            sql_query=sql,
            id_column="ID",
            label_column="Label",
            attribute_mappings={"hasLabel": "label", "hasDescription": "description"},
        )
        assert len(ctx.entity_mappings) == 1
        stored_sql = ctx.entity_mappings[0]["sql_query"]

        # The duplicate "label" column must be renamed, not left colliding
        # with "Label" under case-insensitive resolution.
        assert "label AS Label" in stored_sql
        assert ", label," not in stored_sql, f"unresolved duplicate in: {stored_sql}"

    def test_no_collision_when_columns_are_distinct(self, ctx):
        """Sanity check: distinct column names pass through unchanged."""
        sql = (
            "SELECT viewer_id AS ID, display_name AS Label, viewer_id, display_name, region "
            "FROM catalog.schema.gold_viewer WHERE viewer_id IS NOT NULL"
        )
        tool_submit_entity_mapping(
            ctx,
            class_uri="http://test.org/ontology#Viewer",
            class_name="Viewer",
            sql_query=sql,
            id_column="ID",
            label_column="Label",
            attribute_mappings={"hasRegion": "region"},
        )
        stored_sql = ctx.entity_mappings[0]["sql_query"]
        assert "viewer_id AS ID" in stored_sql
        assert "display_name AS Label" in stored_sql


@pytest.mark.unit
class TestRelationshipMappingDedup:
    def test_relationship_columns_are_deduplicated_too(self, ctx):
        sql = (
            "SELECT viewer_id AS source_id, segment_id AS target_id "
            "FROM catalog.schema.gold_viewer WHERE viewer_id IS NOT NULL AND segment_id IS NOT NULL"
        )
        tool_submit_relationship_mapping(
            ctx,
            property_uri="http://test.org/ontology#belongsTo",
            property_name="belongsTo",
            sql_query=sql,
            source_id_column="source_id",
            target_id_column="target_id",
            domain="http://test.org/ontology#Viewer",
            range_class="http://test.org/ontology#Segment",
        )
        assert len(ctx.relationships) == 1
        stored_sql = ctx.relationships[0]["sql_query"]
        assert "AS source_id" in stored_sql
        assert "AS target_id" in stored_sql


@pytest.mark.unit
class TestEntityMappingMissingAttributes:
    """A silently skipped ontology attribute is reported back to the agent."""

    URI = "http://test.org/ontology#Claim"

    def _submit(self, ctx, **kwargs):
        import json

        ctx.ontology = {"entities": [{"uri": self.URI, "name": "Claim",
                                      "attributes": ["reportNo", "grade", "market"]}]}
        return json.loads(tool_submit_entity_mapping(
            ctx, class_uri=self.URI, class_name="Claim", id_column="ID", label_column="Label",
            sql_query="SELECT claim_id AS ID, claim_id AS Label, grade, market FROM c.s.claim", **kwargs))

    def test_skipped_attribute_is_listed(self, ctx):
        result = self._submit(ctx, attribute_mappings={"grade": "grade", "market": "market"})
        assert result["missing_attributes"] == ["reportNo"]

    def test_skipped_attribute_asks_for_a_resubmit(self, ctx):
        result = self._submit(ctx, attribute_mappings={"grade": "grade", "market": "market"})
        assert "resubmit" in result["action_required"]

    def test_declared_unmapped_attribute_is_not_missing(self, ctx):
        result = self._submit(ctx, attribute_mappings={"grade": "grade", "market": "market"},
                              unmapped_attributes=[{"name": "reportNo", "reason": "no column"}])
        assert "missing_attributes" not in result

    def test_user_excluded_attribute_is_not_missing(self, ctx):
        ctx.entity_mappings = [{"ontology_class": self.URI, "excluded_attributes": ["reportNo"]}]
        result = self._submit(ctx, attribute_mappings={"grade": "grade", "market": "market"})
        assert "missing_attributes" not in result

    def test_complete_mapping_has_no_missing_list(self, ctx):
        result = self._submit(ctx, attribute_mappings={"reportNo": "report_no", "grade": "grade", "market": "market"})
        assert "missing_attributes" not in result

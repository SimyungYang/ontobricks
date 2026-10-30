"""Generate keeps what it can instead of failing the whole run.

Live LGIT failures (2026-10-27/30): every Generate run died because the
endpoint rejected ``response_format`` (see ``test_agent_engine_base``). Once
that schema is gone, the model answers in free text and sometimes writes an
entity as ``[id]`` or by its label, and one bad page used to fail the whole
substage. On top of the paged completion from #206 these tests pin:

* JSON wrapped in prose or a fenced block is still parsed;
* each page's entity references are resolved to bare closed ids, and an item
  still outside the closed set is dropped (reported in ``warnings``), never
  merged;
* a page that fails after earlier pages produced items ends the substage
  with those items plus a warning; only a substage that produced nothing
  fails;
* Stage-1 finalization gets one compact re-emission when truncated.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import requests

from back.objects.ontology.generate.GenerateDraft import (
    CHECKPOINT_DONE,
    GenerateDraft,
    GenerateEntity,
    REVIEWING,
)
from agents.agent_owl_generator import schemas
from agents.agent_owl_generator import staged

pytestmark = pytest.mark.unit


def _answer(content: str, finish_reason: str = "stop") -> dict:
    return {
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _relations_page(*rels, has_more=False) -> dict:
    return _answer(json.dumps({
        "relations": [{"label": l, "domain": d, "range": r} for l, d, r in rels],
        "has_more": has_more,
    }))


def _draft(*, relations_done=False, attributes_done=False, excluded=()):
    candidates = [
        GenerateEntity.new_candidate("Lot", entity_id="cand-lot"),
        GenerateEntity.new_candidate("Work Order", entity_id="cand-wo"),
        *[GenerateEntity.new_candidate(label, entity_id=eid, included=False)
          for eid, label in excluded],
    ]
    draft = GenerateDraft.new(
        source_fingerprint="sha256:fp",
        existing_anchors=[GenerateEntity.locked_anchor("Equipment", "Equipment")],
        candidate_entities=candidates,
        stage=REVIEWING,
    )
    if relations_done:
        draft = draft.with_checkpoint("relations", CHECKPOINT_DONE, result={"relations": []})
    if attributes_done:
        draft = draft.with_checkpoint("attributes", CHECKPOINT_DONE, result={"attributes": []})
    return draft


def _run(fn, responses, draft=None, options=None):
    with patch.object(staged, "call_serving_endpoint") as mock_llm:
        mock_llm.side_effect = responses
        result = fn(host="h", token="t", endpoint_name="e",
                    draft=draft or _draft(), options=options)
    return result, mock_llm


# ---------------------------------------------------------------------------
# Entity references: resolve, else drop the item (not the substage)
# ---------------------------------------------------------------------------


class TestClosureFiltering:
    def test_out_of_closure_item_is_dropped_and_reported(self):
        page = _relations_page(("ships", "cand-lot", "cls-Ghost-999"),
                               ("runsOn", "cand-wo", "Equipment"))
        result, mock_llm = _run(staged.infer_relations, [page])
        assert result.success is True
        assert [r["label"] for r in result.result["relations"]] == ["runsOn"]
        assert any("cls-Ghost-999" in w for w in result.result["warnings"])
        assert mock_llm.call_count == 1

    @pytest.mark.parametrize(
        "written", ["[cand-lot]", '"cand-lot"', "`cand-lot`", "Lot", "lot", " Lot "]
    )
    def test_wrapped_id_or_exact_label_resolves_to_the_bare_id(self, written):
        result, _ = _run(staged.infer_relations,
                         [_relations_page(("usedBy", written, "cand-wo"))])
        assert result.result["relations"][0]["domain"] == "cand-lot"

    def test_multi_word_label_resolves(self):
        result, _ = _run(staged.infer_relations,
                         [_relations_page(("usedBy", "cand-lot", "work order"))])
        assert result.result["relations"][0]["range"] == "cand-wo"

    def test_excluded_candidate_stays_out_of_reach(self):
        draft = _draft(excluded=[("cand-inv", "Invoice")])
        page = _relations_page(("billedVia", "cand-lot", "Invoice"),
                               ("billedVia", "cand-lot", "cand-inv"))
        result, _ = _run(staged.infer_relations, [page], draft=draft)
        assert result.result["relations"] == []

    def test_attribute_and_axiom_references_are_resolved_too(self):
        attrs = _answer(json.dumps({"attributes": [
            {"label": "lotNo", "domain": "[cand-lot]", "datatype": "xsd:string"},
            {"label": "x", "domain": "nowhere", "datatype": "xsd:string"}]}))
        result, _ = _run(staged.infer_attributes, [attrs], draft=_draft(relations_done=True))
        assert [(a["label"], a["domain"]) for a in result.result["attributes"]] == [
            ("lotNo", "cand-lot")]

        axioms = _answer(json.dumps({"axioms": [
            {"kind": "disjointWith", "subject": "Lot", "object": "Equipment"}]}))
        result, _ = _run(staged.infer_axioms, [axioms],
                         draft=_draft(relations_done=True, attributes_done=True))
        assert result.result["axioms"][0]["subject"] == "cand-lot"

    def test_cleaned_result_passes_the_workflow_closure_check(self):
        draft = _draft()
        result, _ = _run(staged.infer_relations,
                         [_relations_page(("x", "[cand-lot]", "ghost"),
                                          ("y", "Lot", "Equipment"))], draft=draft)
        draft.validate_references(schemas.relations_referenced_ids(result.result))

    def test_warnings_key_is_absent_when_nothing_was_dropped(self):
        result, _ = _run(staged.infer_relations,
                         [_relations_page(("runsOn", "cand-wo", "Equipment"))])
        assert "warnings" not in result.result


# ---------------------------------------------------------------------------
# A later page failing keeps the earlier pages
# ---------------------------------------------------------------------------


class TestPartialPages:
    first = staticmethod(lambda: _relations_page(("runsOn", "cand-wo", "Equipment"),
                                                 has_more=True))

    def _assert_kept_first_page(self, result):
        assert result.success is True
        assert [r["label"] for r in result.result["relations"]] == ["runsOn"]
        assert result.result["warnings"]

    def test_transport_error_on_a_later_page(self):
        result, _ = _run(staged.infer_relations,
                         [self.first(), requests.exceptions.ConnectionError("gateway down")])
        self._assert_kept_first_page(result)
        assert any("gateway down" in w for w in result.result["warnings"])

    def test_malformed_later_page(self):
        result, _ = _run(staged.infer_relations, [self.first(), _answer("I am done thinking.")])
        self._assert_kept_first_page(result)

    def test_later_page_truncated_twice(self):
        cut = _answer('{"relations": [', "length")
        result, _ = _run(staged.infer_relations, [self.first(), cut, cut])
        self._assert_kept_first_page(result)

    def test_page_budget_exhausted_keeps_collected_items(self):
        pages = [_relations_page((f"r{i}", "cand-wo", "Equipment"), has_more=True)
                 for i in range(3)]
        result, _ = _run(staged.infer_relations, pages,
                         options={"max_completion_pages": 2})
        assert result.success is True
        assert len(result.result["relations"]) == 2
        assert any("page" in w for w in result.result["warnings"])

    def test_substage_fails_when_nothing_was_produced(self):
        result, _ = _run(staged.infer_relations,
                         [requests.exceptions.ConnectionError("gateway down")])
        assert result.success is False
        assert "gateway down" in result.error

    def test_first_page_malformed_is_still_a_rejection(self):
        result, _ = _run(staged.infer_relations, [_answer("no json here")])
        assert result.success is False
        assert result.rejected is True


# ---------------------------------------------------------------------------
# Stage 1 finalization and parsing without a transport-level schema
# ---------------------------------------------------------------------------


class TestDetectionFinalizationRecovery:
    def _detect(self, responses):
        with patch.object(staged, "call_serving_endpoint") as mock_llm:
            mock_llm.side_effect = responses
            result = staged.detect_entities(
                host="h", token="t", endpoint_name="e", metadata={"tables": []},
                registry={"catalog": "c", "schema": "s", "volume": "v"},
            )
        return result, mock_llm

    def test_truncated_finalization_gets_one_compact_re_emission(self):
        cut = _answer('{"candidate_entities": [{"canonical', "length")
        ok = _answer('{"candidate_entities": [{"canonical_label": "Carrier"}]}')
        result, mock_llm = self._detect([_answer(""), cut, ok])
        assert result.success is True
        assert [c.canonical_label for c in result.candidate_entities] == ["Carrier"]
        assert "compact" in mock_llm.call_args_list[2].args[3][-1]["content"].lower()

    def test_truncated_twice_fails_clearly(self):
        cut = _answer('{"candidate_entities": [{"canonical', "length")
        result, mock_llm = self._detect([_answer(""), cut, cut])
        assert result.success is False
        assert "truncated" in result.error.lower()
        assert mock_llm.call_count == 3


class TestProseWrappedJSON:
    def test_reasoning_preamble_before_the_json_is_tolerated(self):
        text = 'Here are the entities:\n{"candidate_entities": [{"canonical_label": "Lot"}]}'
        assert [c.canonical_label for c in schemas.parse_detection_payload(text)] == ["Lot"]

    def test_fenced_json_after_prose_is_tolerated(self):
        text = 'Sure.\n```json\n{"relations": [], "has_more": false}\n```\nDone.'
        assert schemas.parse_json_object(text) == {"relations": [], "has_more": False}

    def test_text_without_any_json_object_is_still_rejected(self):
        with pytest.raises(schemas.SchemaValidationError):
            schemas.parse_json_object("no structured answer here")

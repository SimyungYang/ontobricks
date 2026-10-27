"""Subclass context for the auto-mapping agent.

The agent only saw each entity's URI, label and attributes, so a subclass
(``parent`` set) was often mapped to every row of its parent's table: nothing
told it that the subclass is a subset, nor which rows (the class description
states the criterion). The payload builders now send ``parent`` and
``description``, and the prompt tells the agent to narrow subclass queries.
"""

import re
from pathlib import Path

import pytest

from agents.agent_auto_assignment.engine import SYSTEM_PROMPT

REPO_ROOT = Path(__file__).resolve().parents[3]
AUTOASSIGN_JS = REPO_ROOT / "src/front/static/mapping/js/mapping-autoassign.js"
DESIGN_JS = REPO_ROOT / "src/front/static/mapping/js/mapping-design.js"


def _entity_payloads(source: str) -> list[str]:
    """Return each object literal that builds an agent entity (has ``attributes:``)."""
    return re.findall(r"return \{\s*uri: [^}]*?attributes: attributes[^}]*\}", source)


@pytest.mark.unit
@pytest.mark.parametrize("js, count", [(AUTOASSIGN_JS, 2), (DESIGN_JS, 1)])
def test_entity_payloads_send_parent_and_description(js, count):
    payloads = _entity_payloads(js.read_text(encoding="utf-8"))
    assert len(payloads) == count
    for body in payloads:
        assert "parent:" in body
        assert "description:" in body


@pytest.mark.unit
def test_prompt_narrows_subclass_queries():
    assert "SUBCLASSES" in SYSTEM_PROMPT
    assert '"parent"' in SYSTEM_PROMPT
    assert "description" in SYSTEM_PROMPT

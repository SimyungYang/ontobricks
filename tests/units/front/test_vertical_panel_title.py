"""Vertical mapping-panel titles read correctly in every script.

The collapsed panel title is set with ``writing-mode: vertical-rl`` and
rotated 180deg. With ``text-orientation: mixed`` CJK glyphs stay upright
before the rotation, so Korean labels ("공장") ended up upside down while
Latin labels looked fine. ``sideways`` lays every glyph on its side first,
so the rotation reads bottom-to-top for all scripts.
"""

import re
from pathlib import Path

import pytest

MAPPING_CSS = Path(__file__).resolve().parents[3] / "src/front/static/global/css/mapping.css"


def _panel_title_rule() -> str:
    css = MAPPING_CSS.read_text(encoding="utf-8")
    match = re.search(r"\.mapping-right-panel \.panel-header h6,[^{]*\{([^}]*)\}", css)
    assert match, "panel title rule not found"
    return match.group(1)


@pytest.mark.unit
def test_rotated_panel_title_lays_all_glyphs_sideways():
    rule = _panel_title_rule()
    assert "writing-mode: vertical-rl" in rule
    assert "rotate(180deg)" in rule
    assert "text-orientation: sideways" in rule

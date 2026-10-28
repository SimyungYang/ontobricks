"""Frozen checkboxes keep showing whether they are checked.

Bootstrap paints a checked ``.form-check-input`` as a white tick
(background-image) on the primary background-color. The read-only gate used
to force ``background-color: #e9ecef`` on every input, which left a white
tick on light grey: checked boxes read as unchecked on a PUBLISHED /
IN-REVIEW version. The grey fill is for text-like fields only; check inputs
are frozen with pointer-events + opacity alone.
"""

import re
from pathlib import Path

import pytest

PERMISSIONS_CSS = (
    Path(__file__).resolve().parents[3] / "src/front/static/global/css/permissions.css"
)
READ_ONLY = "body:is(.read-only-version, .role-viewer, .read-only-locked)"
CHECK_INPUT_SELECTORS = (".js-mcp-tool", ".js-mcp-context", "#mcpToolsSelectAll")

pytestmark = pytest.mark.unit


def _rules() -> list[tuple[list[str], str]]:
    css = re.sub(r"/\*.*?\*/", "", PERMISSIONS_CSS.read_text(encoding="utf-8"), flags=re.S)
    rules = []
    for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        selectors = [s.strip() for s in sel.split(",\n")]
        rules.append(([s for s in selectors if READ_ONLY in s], body))
    return rules


def _grey_fill_selectors() -> list[str]:
    return [
        s
        for selectors, body in _rules()
        if re.search(r"background-color:\s*#e9ecef", body)
        for s in selectors
    ]


def test_grey_fill_skips_checkbox_and_radio_inputs():
    for sel in _grey_fill_selectors():
        if re.search(r"\binput\b", sel):
            assert ':not([type="checkbox"])' in sel and ':not([type="radio"])' in sel, sel


def test_grey_fill_skips_checkbox_only_controls():
    for sel in _grey_fill_selectors():
        assert not sel.endswith(CHECK_INPUT_SELECTORS), sel


def test_check_inputs_are_still_frozen():
    frozen = [
        s
        for selectors, body in _rules()
        if "pointer-events: none" in body
        for s in selectors
    ]
    assert any('input:is([type="checkbox"], [type="radio"])' in s for s in frozen)
    for control in CHECK_INPUT_SELECTORS:
        assert any(s.endswith(control) for s in frozen), control

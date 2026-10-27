"""Explorer marks the entities a search found with a visible halo.

Search seeds were "highlighted" through ``borderColor`` / ``borderSize`` node
attributes, but Sigma's default circle program ignores them, so a found entity
looked like every other node. Seeds now get a DOM halo layer positioned from
Sigma's display data on every render, plus a larger node.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SIGMA_JS = REPO_ROOT / "src/front/static/query/js/query-sigmagraph.js"
SIGMA_CSS = REPO_ROOT / "src/front/static/query/css/query-sigmagraph.css"


def _js() -> str:
    return SIGMA_JS.read_text(encoding="utf-8")


@pytest.mark.unit
def test_halos_follow_every_render():
    js = _js()
    assert "function _drawSeedHalos()" in js
    assert "_renderer.on('afterRender', _drawSeedHalos)" in js


@pytest.mark.unit
def test_halos_use_sigma_display_data_for_position_and_size():
    body = _js().split("function _drawSeedHalos()", 1)[1].split("\n    }\n", 1)[0]
    assert "_highlightedSeeds" in body
    assert "getNodeDisplayData" in body
    assert "framedGraphToViewport" in body
    assert "scaleSize" in body


@pytest.mark.unit
def test_halo_styles_exist_and_ignore_pointer_events():
    css = SIGMA_CSS.read_text(encoding="utf-8")
    assert ".sg-seed-halo-layer" in css
    assert ".sg-seed-halo" in css
    layer = css.split(".sg-seed-halo-layer", 1)[1].split("}", 1)[0]
    assert "pointer-events: none" in layer

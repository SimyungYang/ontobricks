"""Mapping Studio toolbar: one Reset Layout click is one lookup and at most one message.

initMappingDesigner() re-runs on every section switch, mapping save and
schema-drift refresh, while the toolbar buttons are static template markup.
It used to bind them with addEventListener on every run, so the handlers
stacked: a single click on Reset Layout (shuffle icon) fetched the saved layout
N times and, for a domain that never saved one, raised N identical
"No saved layout found" warning toasts.

The handlers are now assigned once per button (onclick, replaced on re-render),
and a domain without a saved layout gets an auto-arrange instead of a warning.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_JS = REPO_ROOT / "src/front/static/mapping/js/mapping-design.js"
ONTOLOGY_MAP_JS = REPO_ROOT / "src/front/static/ontology/js/ontology-map.js"

TOOLBAR_IDS = (
    "mappingMapZoomIn",
    "mappingMapZoomOut",
    "mappingMapResetView",
    "mappingMapResetLayout",
)

# Loads the real mapping-design.js into a sandbox with DOM / d3 / fetch stubs,
# re-binds the toolbar like N designer re-renders would, then clicks.
_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [jsPath, scenarioJson] = process.argv.slice(1);
const scenario = JSON.parse(scenarioJson);

function makeButton(id) {
    const listeners = [];
    return {
        id, dataset: {}, onclick: null, listeners,
        addEventListener(type, fn) { if (type === 'click') listeners.push(fn); },
        click() {
            const ev = { stopPropagation() {}, preventDefault() {} };
            if (this.onclick) this.onclick(ev);
            listeners.forEach(fn => fn(ev));
        },
    };
}
const elements = {};
['mappingMapZoomIn', 'mappingMapZoomOut', 'mappingMapResetView',
 'mappingMapResetLayout'].forEach(id => { elements[id] = makeButton(id); });
elements['mapping-map-container'] = { clientWidth: 1000, clientHeight: 600 };

const log = { fetches: 0, notifications: [], restarts: 0, forces: [], timers: [] };
const simulation = {
    alphaDecay() { return this; }, velocityDecay() { return this; },
    alpha() { return this; },
    force(name) { log.forces.push(name); return this; },
    restart() { log.restarts++; return this; },
};
const chain = () => ({ strength() { return this; }, radius() { return this; } });

const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    document: {
        getElementById: id => elements[id] || null,
        addEventListener() {},
        querySelector() { return null; },
    },
    window: { addEventListener() {} },
    sessionStorage: { getItem() { return null; }, setItem() {} },
    d3: { forceManyBody: chain, forceCollide: chain, forceCenter: chain, zoomIdentity: {} },
    setTimeout: fn => { log.timers.push(fn); return log.timers.length; },
    fetch: async () => {
        log.fetches++;
        return { json: async () => ({ success: true, layout: scenario.layout }) };
    },
    showNotification: (message, type) => log.notifications.push({ message, type }),
    __simulation: simulation,
    __scenario: scenario,
    __log: log,
    __elements: elements,
    __settle: () => new Promise(r => setImmediate(r)),
    __done: () => process.stdout.write(JSON.stringify(log)),
    __fail: e => { process.stderr.write(String((e && e.stack) || e)); process.exit(1); },
};
vm.createContext(sandbox);
const source = fs.readFileSync(jsPath, 'utf8');
vm.runInContext(source + `
;(async () => {
    mappingMapSimulation = __simulation;
    mappingMapNodes = __scenario.nodes.map(id => ({ id, x: 0, y: 0, fx: 0, fy: 0 }));
    for (let i = 0; i < __scenario.binds; i++) bindMappingMapToolbar();
    const btn = __elements.mappingMapResetLayout;
    for (let i = 0; i < __scenario.clicks; i++) btn.click();
    await __settle();
    await __settle();
    __log.listeners = btn.listeners.length;
    __log.hasOnclick = typeof btn.onclick === 'function';
    __log.nodes = mappingMapNodes.map(n => ({ id: n.id, x: n.x, y: n.y, fx: n.fx }));
    __log.inFlight = mappingMapLayoutResetInFlight;
    __log.timers = __log.timers.length;
})().then(__done, __fail);
`, sandbox, { filename: 'mapping-design.js' });
"""


def _run(*, binds=1, clicks=1, layout=None, nodes=("Cathode", "Lot", "Plant")):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    scenario = {
        "binds": binds,
        "clicks": clicks,
        "layout": {} if layout is None else layout,
        "nodes": list(nodes),
    }
    out = subprocess.run(
        [node, "-e", _HARNESS, str(DESIGN_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _js() -> str:
    return DESIGN_JS.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    match = re.search(
        rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source
    )
    assert match, f"{name}() must exist"
    i = match.end()
    depth = 1
    while depth:
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        i += 1
    return source[match.end() : i - 1]


class TestOneClickOneMessage:
    def test_rebinding_many_times_leaves_one_handler(self):
        """12 designer re-renders (the user report) must not stack 12 handlers."""
        result = _run(binds=12, clicks=1)
        assert result["listeners"] == 0
        assert result["hasOnclick"] is True
        assert result["fetches"] == 1

    def test_no_saved_layout_shows_one_non_warning_message(self):
        result = _run(binds=12, clicks=1, layout={})
        assert len(result["notifications"]) == 1
        assert result["notifications"][0]["type"] != "warning"
        assert not any(n["type"] == "warning" for n in result["notifications"])

    def test_no_saved_layout_auto_arranges_the_nodes(self):
        result = _run(layout={})
        assert result["restarts"] == 1
        assert {"charge", "collision", "center"} <= set(result["forces"])
        # Nodes are released for the force layout, then pinned by the timer.
        assert all(n["fx"] is None for n in result["nodes"])
        assert result["timers"] == 1

    def test_empty_saved_positions_count_as_no_layout(self):
        """Ontology Studio's own reset saves `{positions: {}}`."""
        result = _run(layout={"positions": {}})
        assert result["restarts"] == 1
        assert {"charge", "collision", "center"} <= set(result["forces"])
        assert len(result["notifications"]) == 1

    def test_rapid_repeat_clicks_are_deduped(self):
        result = _run(binds=5, clicks=4, layout={})
        assert result["fetches"] == 1
        assert len(result["notifications"]) == 1
        assert result["inFlight"] is False

    def test_saved_layout_is_restored_without_a_warning(self):
        layout = {"positions": {"Cathode": {"x": 11, "y": 22}, "Lot": {"x": 33, "y": 44}}}
        result = _run(binds=3, clicks=1, layout=layout)
        nodes = {n["id"]: n for n in result["nodes"]}
        assert (nodes["Cathode"]["x"], nodes["Cathode"]["y"]) == (11, 22)
        assert (nodes["Lot"]["x"], nodes["Lot"]["y"]) == (33, 44)
        # Missing node falls back to the default grid slot (index 2).
        assert (nodes["Plant"]["x"], nodes["Plant"]["y"]) == (400, 100)
        assert result["forces"] == []
        assert [n["type"] for n in result["notifications"]] == ["info"]


class TestBindingIsIdempotent:
    def test_designer_init_delegates_to_the_toolbar_binder(self):
        body = _function_body(_js(), "initMappingDesigner")
        assert "bindMappingMapToolbar()" in body
        for btn_id in TOOLBAR_IDS:
            assert f"getElementById('{btn_id}')?.addEventListener" not in body

    def test_toolbar_binder_assigns_onclick(self):
        body = _function_body(_js(), "bindMappingMapToolbar")
        assert "addEventListener" not in body
        for btn_id in TOOLBAR_IDS:
            assert f"getElementById('{btn_id}')" in body
        assert body.count(".onclick =") == len(TOOLBAR_IDS)

    def test_toolbar_handlers_use_current_map_state(self):
        """Handlers must not close over one render's svg / nodes."""
        body = _function_body(_js(), "bindMappingMapToolbar")
        assert "mappingMapSvg" in body
        assert "mappingMapZoom" in body
        reset = _function_body(_js(), "resetMappingMapLayout")
        assert "mappingMapNodes" in reset
        assert "'warning'" not in reset

    def test_ontology_map_toolbar_does_not_stack_listeners(self):
        """Ontology Studio's graph had the same per-render addEventListener."""
        body = _function_body(
            ONTOLOGY_MAP_JS.read_text(encoding="utf-8"), "initOntologyMap"
        )
        for btn_id in ("mapZoomIn", "mapZoomOut", "mapResetView", "mapResetLayout"):
            assert f"getElementById('{btn_id}')?.addEventListener" not in body
            assert f"getElementById('{btn_id}')" in body

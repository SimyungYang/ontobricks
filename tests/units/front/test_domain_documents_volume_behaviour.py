"""Behaviour of the Knowledge Store UC Volume import UI, executed with node.

``test_domain_documents_ui.py`` pins the markup contract; these tests run
``VolumeImport`` against a tiny element stub to check the read-only gate, the
POST payload, folder navigation, and HTML escaping of volume entries.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCUMENTS_JS = REPO_ROOT / "src/front/static/domain/js/domain-documents.js"
VOLUME_JS = REPO_ROOT / "src/front/static/domain/js/domain-documents-volume.js"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("node") is None, reason="node is required"),
]

_HARNESS = r"""
const fs = require('fs');
const elements = {};
function el(id) {
    if (!elements[id]) {
        elements[id] = {
            id, innerHTML: '', textContent: '', value: '', disabled: false,
            classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
            addEventListener() {}, setAttribute() {}, appendChild() {},
        };
    }
    return elements[id];
}
global.window = { isActiveVersion: __ACTIVE__ };
global.document = {
    getElementById: el,
    addEventListener() {},
    querySelectorAll: () => [],
    createElement: () => el('_tmp'),
    cookie: '',
};
const calls = [];
const notes = [];
global.fetch = async (url, init) => {
    calls.push({ url, init });
    return { ok: true, json: async () => (__RESPONSE__) };
};
global.showNotification = (msg, type) => notes.push([msg, type]);
global.showDeleteConfirm = async () => true;
eval(fs.readFileSync(__DOCS__, 'utf8') + '\n' + fs.readFileSync(__VOLUME__, 'utf8')
     + '\nglobal.VolumeImport = VolumeImport; global.DocManager = DocManager;');
DocManager.refreshList = () => calls.push({ url: 'refresh' });
(async () => {
    const out = await (__SCENARIO__)();
    process.stdout.write(JSON.stringify({ out, calls, notes, elements }));
})();
"""


def _run(scenario: str, *, active: bool = True, response=None) -> dict:
    script = (
        _HARNESS.replace("__ACTIVE__", "true" if active else "false")
        .replace("__RESPONSE__", json.dumps(response or {"success": True, "results": []}))
        .replace("__DOCS__", json.dumps(str(DOCUMENTS_JS)))
        .replace("__VOLUME__", json.dumps(str(VOLUME_JS)))
        .replace("__SCENARIO__", scenario)
    )
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_import_posts_paths_and_refreshes_the_shared_list():
    result = _run(
        "async () => VolumeImport.importPaths(['/Volumes/c/s/v/a.md'])",
        response={
            "success": True,
            "message": "1/1 file(s) imported",
            "results": [{"success": True, "parse_status": "ready"}],
        },
    )
    post = result["calls"][0]
    assert post["url"] == "/domain/documents/import-volume"
    assert post["init"]["method"] == "POST"
    assert json.loads(post["init"]["body"]) == {"paths": ["/Volumes/c/s/v/a.md"]}
    assert result["calls"][-1] == {"url": "refresh"}
    assert result["notes"] == [["1/1 file(s) imported", "success"]]


def test_partial_import_is_reported_as_warning():
    result = _run(
        "async () => VolumeImport.importPaths(['/Volumes/c/s/v'])",
        response={
            "success": True,
            "truncated": True,
            "message": "50/50 file(s) imported. Only the first 50 supported files were imported",
            "results": [{"success": True, "parse_status": "pending"}],
        },
    )
    assert result["notes"][0][1] == "warning"


def test_read_only_version_blocks_import_and_browse():
    result = _run(
        "async () => { VolumeImport.toggleBrowser(); await VolumeImport.importPaths(['/Volumes/c/s/v/a.md']); }",
        active=False,
    )
    assert result["calls"] == []


def test_typed_path_must_start_with_volumes():
    result = _run(
        "async () => { document.getElementById('docVolumePathInput').value = 'dbfs:/x'; VolumeImport.importTyped(); }"
    )
    assert result["calls"] == []
    assert result["notes"][0][1] == "warning"


def test_parent_path_stops_at_the_volume_root():
    result = _run(
        "async () => [VolumeImport.parentPath('/Volumes/c/s/v/a/b'), "
        "VolumeImport.parentPath('/Volumes/c/s/v'), VolumeImport.isVolumeRoot('/Volumes/c/s/v')]"
    )
    assert result["out"] == ["/Volumes/c/s/v/a", "/Volumes/c/s/v", True]


def test_browse_renders_escaped_entries_and_flags():
    items = [
        {"name": "sub", "path": "/Volumes/c/s/v/sub", "is_directory": True, "size": 0,
         "supported": True, "too_large": False},
        {"name": "<img src=x onerror=alert(1)>.md", "path": "/Volumes/c/s/v/<img>.md",
         "is_directory": False, "size": 3, "supported": True, "too_large": False},
        {"name": "big.pdf", "path": "/Volumes/c/s/v/big.pdf", "is_directory": False,
         "size": 11 * 1024 * 1024, "supported": True, "too_large": True},
    ]
    result = _run(
        "async () => VolumeImport.open('/Volumes/c/s/v')",
        response={"success": True, "path": "/Volumes/c/s/v", "items": items, "truncated": False},
    )
    assert result["calls"][0]["url"] == "/domain/documents/volume/browse?path=%2FVolumes%2Fc%2Fs%2Fv"
    html = result["elements"]["docVolumeEntries"]["innerHTML"]
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;.md" in html
    assert 'data-volume-dir="/Volumes/c/s/v/sub"' in html
    assert "Over 10 MB" in html
    assert result["elements"]["docVolumeImportFolderBtn"]["disabled"] is False
    assert result["elements"]["docVolumeUpBtn"]["disabled"] is True

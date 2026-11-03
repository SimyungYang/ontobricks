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
            id, innerHTML: '', textContent: '', value: '', disabled: false, style: {}, attrs: {},
            classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
            addEventListener() {}, appendChild() {},
            setAttribute(name, value) { this.attrs[name] = value; },
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
const progressSeen = [];
global.fetch = async (url, init) => {
    calls.push({ url, init });
    return { ok: true, json: async () => (__RESPONSE__) };
};
// Stand-in for task-tracker.js: replay progress snapshots, then resolve.
global.waitForTask = async (taskId, onProgress) => {
    calls.push({ url: `/tasks/${taskId}` });
    for (const result of __PROGRESS__) {
        onProgress({ status: 'running', result });
        progressSeen.push({
            count: el('docVolumeProgressCount').textContent,
            width: el('docVolumeProgressFill').style.width,
        });
    }
    return { status: 'completed', result: __FINAL__ };
};
global.showNotification = (msg, type) => notes.push([msg, type]);
global.showDeleteConfirm = async () => true;
eval(fs.readFileSync(__DOCS__, 'utf8') + '\n' + fs.readFileSync(__VOLUME__, 'utf8')
     + '\nglobal.VolumeImport = VolumeImport; global.DocManager = DocManager;');
DocManager.refreshList = () => calls.push({ url: 'refresh' });
(async () => {
    const out = await (__SCENARIO__)();
    process.stdout.write(JSON.stringify({ out, calls, notes, elements, progressSeen }));
})();
"""

STARTED = {"success": True, "task_id": "t1", "message": "Import started"}


def _summary(**overrides):
    base = {
        "total": 120, "processed": 120, "imported": 120, "failed": 0,
        "skipped_over_cap": 0, "listing_truncated": False, "batches": 3,
        "batches_done": 3, "failures": [], "remaining": [], "stopped_reason": "",
    }
    return {**base, **overrides}


def _run(scenario: str, *, active: bool = True, response=None, progress=(), final=None) -> dict:
    script = (
        _HARNESS.replace("__ACTIVE__", "true" if active else "false")
        .replace("__RESPONSE__", json.dumps(response or STARTED))
        .replace("__PROGRESS__", json.dumps(list(progress)))
        .replace("__FINAL__", json.dumps(final or _summary()))
        .replace("__DOCS__", json.dumps(str(DOCUMENTS_JS)))
        .replace("__VOLUME__", json.dumps(str(VOLUME_JS)))
        .replace("__SCENARIO__", scenario)
    )
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


IMPORT_ONE = "async () => VolumeImport.importPaths(['/Volumes/c/s/v'])"


def test_import_posts_paths_then_follows_the_task():
    result = _run(IMPORT_ONE)
    post = result["calls"][0]
    assert post["url"] == "/domain/documents/import-volume"
    assert post["init"]["method"] == "POST"
    assert json.loads(post["init"]["body"]) == {"paths": ["/Volumes/c/s/v"]}
    assert result["calls"][1] == {"url": "/tasks/t1"}
    assert result["calls"][-1] == {"url": "refresh"}
    assert result["notes"] == [["Import complete", "success"]]


def test_progress_is_shown_and_the_list_refreshes_per_batch():
    progress = [
        _summary(processed=50, imported=50, batches_done=1),
        _summary(processed=60, imported=60, batches_done=1),
        _summary(processed=100, imported=100, batches_done=2),
    ]
    result = _run(IMPORT_ONE, progress=progress)
    assert result["progressSeen"] == [
        {"count": "50 / 120", "width": "42%"},
        {"count": "60 / 120", "width": "50%"},
        {"count": "100 / 120", "width": "83%"},
    ]
    # One refresh per finished batch (1, 2) plus the final one.
    assert result["calls"].count({"url": "refresh"}) == 3


def test_summary_reports_failures_with_reasons_and_skipped_over_cap():
    final = _summary(
        total=500, processed=500, imported=499, failed=1, skipped_over_cap=100,
        failures=[{"filename": "<b>x</b>.pdf", "source_path": "/Volumes/c/s/v/<b>x</b>.pdf",
                   "message": "Unsupported document type"}],
    )
    result = _run(IMPORT_ONE, final=final)
    html = result["elements"]["docVolumeSummary"]["innerHTML"]
    assert "Skipped (over the per-import limit)" in html
    assert "<strong>100</strong>" in html
    assert "<strong>499</strong>" in html
    assert "&lt;b&gt;x&lt;/b&gt;.pdf" in html and "<b>x</b>" not in html
    assert "Unsupported document type" in html
    assert result["notes"] == [["Import finished with some files not imported", "warning"]]


def test_stopped_job_lists_remaining_files():
    final = _summary(
        processed=60, imported=60, remaining=["/Volumes/c/s/v/d060.md"],
        stopped_reason="Your Databricks sign-in expired",
    )
    result = _run(IMPORT_ONE, final=final)
    html = result["elements"]["docVolumeSummary"]["innerHTML"]
    assert "Not imported because the job stopped" in html
    assert "/Volumes/c/s/v/d060.md" in html
    assert result["notes"][0] == [
        "Import stopped before all files were read: Your Databricks sign-in expired",
        "warning",
    ]


def test_rejected_start_is_reported_without_polling():
    result = _run(IMPORT_ONE, response={"success": False, "message": "Domain not saved"})
    assert {"url": "/tasks/t1"} not in result["calls"]
    assert result["notes"] == [["Import failed: Domain not saved", "error"]]


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

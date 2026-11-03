"""UI contracts for the Knowledge Store (parse status, retry, purge, view)."""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCUMENTS_JS = REPO_ROOT / "src/front/static/domain/js/domain-documents.js"
DOCUMENTS_HTML = (
    REPO_ROOT / "src/front/templates/partials/domain/_domain_documents.html"
)
WIZARD_JS = REPO_ROOT / "src/front/static/ontology/js/ontology-wizard.js"
MENU_CONFIG = REPO_ROOT / "src/front/config/menu_config.json"
UTILS_JS = REPO_ROOT / "src/front/static/global/js/utils.js"


def test_feature_is_named_knowledge_store():
    html = DOCUMENTS_HTML.read_text(encoding="utf-8")
    assert "Knowledge Store" in html
    assert "Document Management" not in html
    menu = MENU_CONFIG.read_text(encoding="utf-8")
    ids = json.loads(menu)
    # The menu id stays 'documents'; only the label renames.
    assert '"label": "Knowledge Store"' in menu
    assert '"id": "documents"' in menu
    assert ids is not None


def test_no_volume_location_banner():
    # The Knowledge Store itself is Volume-free (Lakebase rows); a Volume only
    # appears as an optional *import source*, never as the storage location.
    html = DOCUMENTS_HTML.read_text(encoding="utf-8")
    assert "docLocationBanner" not in html
    assert "Unity Catalog volume" not in html
    js = DOCUMENTS_JS.read_text(encoding="utf-8")
    assert "loadVolumeLocation" not in js


# -- Import from a Unity Catalog Volume ------------------------------

VOLUME_JS = REPO_ROOT / "src/front/static/domain/js/domain-documents-volume.js"
DOMAIN_HTML = REPO_ROOT / "src/front/templates/domain.html"
PERMISSIONS_CSS = REPO_ROOT / "src/front/static/global/css/permissions.css"


def test_volume_import_controls_sit_in_the_upload_card():
    html = DOCUMENTS_HTML.read_text(encoding="utf-8")
    for element_id in (
        "docVolumeImport",
        "docVolumePathInput",
        "docVolumeBrowseBtn",
        "docVolumeImportPathBtn",
        "docVolumeCatalog",
        "docVolumeSchema",
        "docVolumeVolume",
        "docVolumeEntries",
        "docVolumeImportSelectedBtn",
        "docVolumeImportFolderBtn",
    ):
        assert f'id="{element_id}"' in html, element_id
    assert "Import from Unity Catalog Volume" in html
    # Upload card precedes the Parsed Documents card that shows the status list.
    assert html.index('id="docVolumeImport"') < html.index("Parsed Documents")


def test_volume_import_script_loads_after_documents_script():
    html = DOMAIN_HTML.read_text(encoding="utf-8")
    assert html.index("domain/js/domain-documents.js") < html.index(
        "domain/js/domain-documents-volume.js"
    )


def test_volume_import_uses_user_scoped_endpoints_and_shared_list():
    js = VOLUME_JS.read_text(encoding="utf-8")
    assert "'/domain/documents/import-volume'" in js
    assert "/domain/documents/volume/browse?path=${encodeURIComponent(path)}" in js
    assert "JSON.stringify({ paths })" in js
    assert "method: 'POST'" in js
    # Imported rows show up in the same Ready/status list as uploads.
    assert "DocManager.refreshList()" in js
    # Existing catalog / schema / volume listing APIs are reused.
    for url in ("/settings/catalogs", "/settings/schemas/", "/settings/volumes/"):
        assert url in js


def test_volume_import_follows_a_background_task():
    html = DOCUMENTS_HTML.read_text(encoding="utf-8")
    for element_id in (
        "docVolumeProgress",
        "docVolumeProgressBar",
        "docVolumeProgressFill",
        "docVolumeProgressCount",
        "docVolumeSummary",
    ):
        assert f'id="{element_id}"' in html, element_id
    # Progress and summary live inside the gated import block.
    assert html.index('id="docVolumeImport"') < html.index('id="docVolumeSummary"')
    js = VOLUME_JS.read_text(encoding="utf-8")
    assert "waitForTask(taskId, task =>" in js
    assert "summary.batches_done" in js
    tracker = (REPO_ROOT / "src/front/static/global/js/task-tracker.js").read_text(
        encoding="utf-8"
    )
    assert "'knowledge_store_import': '/domain#documents'" in tracker


def test_volume_import_is_gated_like_upload():
    css = PERMISSIONS_CSS.read_text(encoding="utf-8")
    gate = "body:is(.read-only-version, .role-viewer, .read-only-locked)"
    assert f"{gate} #docDropZone" in css
    assert f"{gate} #docVolumeImport" in css
    js = VOLUME_JS.read_text(encoding="utf-8")
    assert "return window.isActiveVersion !== false;" in js
    assert "if (!this.isEditable() || this.busy || paths.length === 0) return;" in js


def test_volume_paths_never_reach_inline_handlers():
    js = VOLUME_JS.read_text(encoding="utf-8")
    assert "onclick=" not in js
    assert "onchange=" not in js
    assert 'data-volume-dir="${safePath}"' in js
    assert 'data-volume-file="${safePath}"' in js
    assert "<span data-i18n-skip class=\"text-truncate\">${safeName}</span>" in js
    assert "option.setAttribute('data-i18n-skip', '');" in js


def test_upload_enforces_10mb_client_guard():
    js = DOCUMENTS_JS.read_text(encoding="utf-8")
    assert "MAX_UPLOAD_BYTES = 10 * 1024 * 1024" in js
    assert "f.size > MAX_UPLOAD_BYTES" in js
    html = DOCUMENTS_HTML.read_text(encoding="utf-8")
    assert "10" in html and "MB" in html


def test_multi_select_purge_control_present():
    html = DOCUMENTS_HTML.read_text(encoding="utf-8")
    assert 'id="docPurgeSelectedBtn"' in html
    assert "Purge selected" in html
    js = DOCUMENTS_JS.read_text(encoding="utf-8")
    assert "purgeSelected()" in js
    assert "toggleSelectAll(" in js
    assert "JSON.stringify({ filenames })" in js


def test_preview_serves_parsed_text_only():
    js = UTILS_JS.read_text(encoding="utf-8")
    # No binary streaming branches remain — parsed text/markdown only.
    assert "doc-preview-iframe" not in js
    assert "doc-preview-image" not in js
    assert "documents/preview/" in js


def test_documents_ui_renders_all_parse_states():
    js = DOCUMENTS_JS.read_text(encoding="utf-8")
    assert "parseStatusBadge(" in js
    assert "Parsing" in js
    assert "Ready" in js
    assert "Parse failed" in js
    assert "bi-hourglass-split" in js
    assert "bi-check-circle" in js
    assert "bi-exclamation-triangle" in js


def test_pending_polling_is_single_and_stops_when_terminal():
    js = DOCUMENTS_JS.read_text(encoding="utf-8")
    assert "parsePollTimer: null" in js
    assert "clearTimeout(this.parsePollTimer)" in js
    assert "files.some(file => file.parse_status === 'pending')" in js
    assert "setTimeout(() => this.refreshList(), 2000)" in js


def test_failed_document_has_retry_action():
    js = DOCUMENTS_JS.read_text(encoding="utf-8")
    assert "retryParse(filename)" in js
    assert "'/domain/documents/retry-parse'" in js
    assert "JSON.stringify({ filename })" in js
    assert "Retry parsing" in js


def test_status_region_is_accessible():
    html = DOCUMENTS_HTML.read_text(encoding="utf-8")
    assert 'id="docFileList"' in html
    assert 'aria-live="polite"' in html
    assert "Files are parsed once after upload" in html


def test_document_preview_filename_is_keyboard_accessible():
    js = DOCUMENTS_JS.read_text(encoding="utf-8")
    assert 'tabindex="0"' in js
    assert "event.key === 'Enter' || event.key === ' '" in js


def test_wizard_pending_documents_use_hourglass_status_icon():
    js = WIZARD_JS.read_text(encoding="utf-8")
    assert "file.parse_status === 'pending' ? 'bi-hourglass-split'" in js


def test_generate_filters_unready_selected_documents():
    js = WIZARD_JS.read_text(encoding="utf-8")
    assert "getSelectedDocumentFiles()" in js
    assert "file.parse_status === 'ready'" in js
    assert "Document parsing is not ready" in js
    assert "unavailableDocuments.map(file => file.name)" in js

// =====================================================
// KNOWLEDGE STORE — Import from a Unity Catalog Volume
// =====================================================
// Folder listings and file reads run server-side with the signed-in user's
// own token, so only files the user can open in Unity Catalog are importable.
// Imported files go through the same parse-once pipeline as local uploads.
// Depends on domain-documents.js (DocManager, escapeDocHtml, fileIcon, formatSize).

const VolumeImport = {
    currentPath: '',
    selected: new Set(),
    busy: false,
    catalogsLoaded: false,
    folderImportable: false,

    init() {
        const browseBtn = document.getElementById('docVolumeBrowseBtn');
        if (!browseBtn) return;
        const input = document.getElementById('docVolumePathInput');
        const catalog = document.getElementById('docVolumeCatalog');
        const schema = document.getElementById('docVolumeSchema');
        const volume = document.getElementById('docVolumeVolume');
        const entries = document.getElementById('docVolumeEntries');

        browseBtn.addEventListener('click', () => this.toggleBrowser());
        document.getElementById('docVolumeImportPathBtn').addEventListener('click', () => this.importTyped());
        input.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); this.importTyped(); }
        });
        catalog.addEventListener('change', () => this.loadSchemas(catalog.value));
        schema.addEventListener('change', () => this.loadVolumes(catalog.value, schema.value));
        volume.addEventListener('change', () => {
            if (volume.value) this.open(`/Volumes/${catalog.value}/${schema.value}/${volume.value}`);
        });
        document.getElementById('docVolumeUpBtn').addEventListener('click', () => this.open(this.parentPath(this.currentPath)));
        document.getElementById('docVolumeImportSelectedBtn').addEventListener('click', () => this.importPaths(Array.from(this.selected)));
        document.getElementById('docVolumeImportFolderBtn').addEventListener('click', () => this.importPaths([this.currentPath]));

        // Delegated handlers: paths travel in data attributes, never in inline JS.
        entries.addEventListener('click', e => {
            const dir = e.target.closest('[data-volume-dir]');
            if (dir) this.open(dir.dataset.volumeDir);
        });
        entries.addEventListener('change', e => {
            const box = e.target.closest('input[data-volume-file]');
            if (box) this.toggle(box.dataset.volumeFile, box.checked);
        });
    },

    isEditable() {
        return window.isActiveVersion !== false;
    },

    toggleBrowser() {
        if (!this.isEditable()) return;
        const panel = document.getElementById('docVolumeBrowser');
        const opening = panel.classList.contains('d-none');
        panel.classList.toggle('d-none', !opening);
        document.getElementById('docVolumeBrowseBtn').setAttribute('aria-expanded', String(opening));
        if (opening && !this.catalogsLoaded) this.loadCatalogs();
    },

    // -- catalog / schema / volume pickers (existing /settings listing APIs) --

    async fetchNames(url, key) {
        const resp = await fetch(url, { credentials: 'same-origin' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.message || 'Request failed');
        return data[key] || [];
    },

    fillSelect(select, names, placeholder) {
        select.innerHTML = '';
        const first = document.createElement('option');
        first.value = '';
        first.textContent = names.length ? placeholder : 'None found';
        select.appendChild(first);
        names.forEach(name => {
            const option = document.createElement('option');
            option.value = name;
            option.textContent = name;
            option.setAttribute('data-i18n-skip', '');
            select.appendChild(option);
        });
        select.disabled = names.length === 0;
    },

    resetSelect(id, placeholder) {
        const select = document.getElementById(id);
        select.innerHTML = '';
        const first = document.createElement('option');
        first.value = '';
        first.textContent = placeholder;
        select.appendChild(first);
        select.disabled = true;
    },

    async loadCatalogs() {
        const select = document.getElementById('docVolumeCatalog');
        try {
            const names = await this.fetchNames('/settings/catalogs', 'catalogs');
            this.fillSelect(select, names, 'Select catalog...');
            this.catalogsLoaded = true;
        } catch (err) {
            showNotification('Could not load catalogs: ' + err.message, 'error');
        }
    },

    async loadSchemas(catalog) {
        this.resetSelect('docVolumeSchema', 'Select schema...');
        this.resetSelect('docVolumeVolume', 'Select volume...');
        if (!catalog) return;
        try {
            const names = await this.fetchNames(`/settings/schemas/${encodeURIComponent(catalog)}`, 'schemas');
            this.fillSelect(document.getElementById('docVolumeSchema'), names, 'Select schema...');
        } catch (err) {
            showNotification('Could not load schemas: ' + err.message, 'error');
        }
    },

    async loadVolumes(catalog, schema) {
        this.resetSelect('docVolumeVolume', 'Select volume...');
        if (!catalog || !schema) return;
        try {
            const names = await this.fetchNames(
                `/settings/volumes/${encodeURIComponent(catalog)}/${encodeURIComponent(schema)}`, 'volumes');
            this.fillSelect(document.getElementById('docVolumeVolume'), names, 'Select volume...');
        } catch (err) {
            showNotification('Could not load volumes: ' + err.message, 'error');
        }
    },

    // -- folder browsing (user identity, server-side) --------------------

    isVolumeRoot(path) {
        return path.split('/').filter(Boolean).length <= 4; // Volumes/catalog/schema/volume
    },

    parentPath(path) {
        if (this.isVolumeRoot(path)) return path;
        return path.slice(0, path.lastIndexOf('/'));
    },

    async open(path) {
        const list = document.getElementById('docVolumeEntries');
        list.innerHTML = '<div class="text-muted small px-2 py-2"><span class="spinner-border spinner-border-sm me-1"></span> Loading...</div>';
        this.selected.clear();
        this.updateSelectionUI();
        try {
            const resp = await fetch(`/domain/documents/volume/browse?path=${encodeURIComponent(path)}`,
                { credentials: 'same-origin' });
            const result = await resp.json();
            if (!resp.ok || !result.success) {
                throw new Error(result.message || 'Could not list the folder');
            }
            this.currentPath = result.path;
            document.getElementById('docVolumeCurrentPath').textContent = result.path;
            document.getElementById('docVolumeUpBtn').disabled = this.isVolumeRoot(result.path);
            this.folderImportable = result.items.some(item => this.isImportable(item));
            document.getElementById('docVolumeImportFolderBtn').disabled = !this.folderImportable || this.busy;
            this.renderEntries(result.items, result.truncated);
        } catch (err) {
            this.folderImportable = false;
            document.getElementById('docVolumeImportFolderBtn').disabled = true;
            list.innerHTML = `<div class="text-danger small px-2 py-2">
                <i class="bi bi-exclamation-triangle me-1"></i>${escapeDocHtml(err.message)}</div>`;
        }
    },

    isImportable(item) {
        return !item.is_directory && item.supported && !item.too_large;
    },

    entryBadge(item) {
        if (!item.supported) return '<span class="badge text-bg-secondary ms-auto">Unsupported type</span>';
        if (item.too_large) return '<span class="badge text-bg-warning ms-auto">Over 10 MB</span>';
        return '';
    },

    renderEntries(items, truncated) {
        const list = document.getElementById('docVolumeEntries');
        if (!items.length) {
            list.innerHTML = '<div class="text-muted small fst-italic px-2 py-2">This folder is empty.</div>';
            return;
        }
        const rows = items.map(item => {
            const safeName = escapeDocHtml(item.name);
            const safePath = escapeDocHtml(item.path);
            if (item.is_directory) {
                return `<button type="button" class="list-group-item list-group-item-action d-flex align-items-center gap-2 px-2 py-1 small"
                            data-volume-dir="${safePath}">
                        <i class="bi bi-folder text-warning"></i><span data-i18n-skip class="text-truncate">${safeName}</span>
                    </button>`;
            }
            const importable = this.isImportable(item);
            return `<label class="list-group-item d-flex align-items-center gap-2 px-2 py-1 small${importable ? '' : ' text-muted'}">
                    <input type="checkbox" class="form-check-input mt-0" data-volume-file="${safePath}"
                           ${importable ? '' : 'disabled'} aria-label="${safeName}">
                    <i class="bi ${fileIcon(item.name)}"></i>
                    <span data-i18n-skip class="text-truncate">${safeName}</span>
                    <span class="text-muted text-nowrap">(${formatSize(item.size || 0)})</span>
                    ${this.entryBadge(item)}
                </label>`;
        });
        if (truncated) {
            rows.push('<div class="text-muted small fst-italic px-2 py-1">Only the first 500 entries are shown.</div>');
        }
        list.innerHTML = rows.join('');
    },

    toggle(path, checked) {
        if (checked) this.selected.add(path); else this.selected.delete(path);
        this.updateSelectionUI();
    },

    updateSelectionUI() {
        document.getElementById('docVolumeSelectedCount').textContent = String(this.selected.size);
        document.getElementById('docVolumeImportSelectedBtn').disabled = this.selected.size === 0 || this.busy;
    },

    // -- import ------------------------------------------------------------

    importTyped() {
        const value = document.getElementById('docVolumePathInput').value.trim();
        if (!value.startsWith('/Volumes/')) {
            showNotification('Enter a path that starts with /Volumes/', 'warning');
            return;
        }
        this.importPaths([value]);
    },

    setBusy(busy) {
        this.busy = busy;
        document.getElementById('docVolumeImportPathBtn').disabled = busy;
        document.getElementById('docVolumeImportFolderBtn').disabled = busy || !this.folderImportable;
        this.updateSelectionUI();
    },

    // The server runs the import as a background task (batches of 50) and
    // returns a task_id at once; progress is polled from /tasks/{id}.
    async importPaths(paths) {
        if (!this.isEditable() || this.busy || paths.length === 0) return;
        this.setBusy(true);
        document.getElementById('docVolumeSummary').classList.add('d-none');
        this.showProgress({});
        try {
            const resp = await fetch('/domain/documents/import-volume', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paths }),
                credentials: 'same-origin',
            });
            const result = await resp.json();
            if (!resp.ok || !result.success) throw new Error(result.message || 'Request failed');
            this.selected.clear();
            document.querySelectorAll('#docVolumeEntries input[data-volume-file]').forEach(box => { box.checked = false; });
            if (typeof window.refreshTasks === 'function') window.refreshTasks();
            const task = await this.followTask(result.task_id);
            this.finish(task.result || {});
        } catch (err) {
            showNotification('Import failed: ' + err.message, 'error');
        } finally {
            this.hideProgress();
            this.setBusy(false);
            DocManager.refreshList();
        }
    },

    followTask(taskId) {
        let batchesSeen = 0;
        return waitForTask(taskId, task => {
            const summary = task.result || {};
            this.showProgress(summary);
            // Refresh the shared document list each time a batch of 50 finishes.
            if ((summary.batches_done || 0) > batchesSeen) {
                batchesSeen = summary.batches_done;
                DocManager.refreshList();
            }
        });
    },

    showProgress(summary) {
        const processed = summary.processed || 0;
        const total = summary.total || 0;
        const pct = total ? Math.round((processed * 100) / total) : 0;
        document.getElementById('docVolumeProgress').classList.remove('d-none');
        document.getElementById('docVolumeProgressCount').textContent = total ? `${processed} / ${total}` : '';
        const bar = document.getElementById('docVolumeProgressBar');
        bar.setAttribute('aria-valuenow', String(pct));
        document.getElementById('docVolumeProgressFill').style.width = `${pct}%`;
    },

    hideProgress() {
        document.getElementById('docVolumeProgress').classList.add('d-none');
    },

    finish(summary) {
        this.renderSummary(summary);
        if (summary.stopped_reason) {
            showNotification('Import stopped before all files were read: ' + summary.stopped_reason, 'warning');
        } else if (summary.failed || summary.skipped_over_cap) {
            showNotification('Import finished with some files not imported', 'warning');
        } else {
            showNotification('Import complete', 'success');
        }
    },

    summaryLine(icon, label, value) {
        return `<div><i class="bi ${icon} me-1"></i><span>${label}</span>: <strong>${value}</strong></div>`;
    },

    pathList(label, rows) {
        if (!rows.length) return '';
        return `<details class="mt-1"><summary>${label}</summary>
            <ul class="doc-volume-report mb-0 ps-3">${rows.join('')}</ul></details>`;
    },

    renderSummary(summary) {
        const failures = (summary.failures || []).map(f => `<li>
            <span data-i18n-skip class="fw-semibold">${escapeDocHtml(f.filename || f.source_path || '')}</span>
            — <span>${escapeDocHtml(f.message || '')}</span></li>`);
        const remaining = (summary.remaining || []).map(path =>
            `<li><span data-i18n-skip>${escapeDocHtml(path)}</span></li>`);
        const lines = [
            this.summaryLine('bi-check-circle text-success', 'Imported', summary.imported || 0),
            this.summaryLine('bi-x-circle text-danger', 'Failed', summary.failed || 0),
        ];
        if (summary.skipped_over_cap) {
            const count = `${summary.skipped_over_cap}${summary.listing_truncated ? '+' : ''}`;
            lines.push(this.summaryLine('bi-dash-circle text-warning', 'Skipped (over the per-import limit)', count));
        }
        if (remaining.length) {
            lines.push(this.summaryLine('bi-exclamation-triangle text-danger', 'Not imported because the job stopped', remaining.length));
            lines.push(`<div class="text-muted">${escapeDocHtml(summary.stopped_reason || '')}</div>`);
        }
        const warn = summary.failed || summary.skipped_over_cap || remaining.length;
        const box = document.getElementById('docVolumeSummary');
        box.innerHTML = `<div class="alert ${warn ? 'alert-warning' : 'alert-success'} py-2 small mb-0">
                <div class="fw-semibold mb-1">Import summary</div>
                ${lines.join('')}
                ${this.pathList('Show failed files', failures)}
                ${this.pathList('Show remaining files', remaining)}
            </div>`;
        box.classList.remove('d-none');
    },
};

document.addEventListener('DOMContentLoaded', () => VolumeImport.init());

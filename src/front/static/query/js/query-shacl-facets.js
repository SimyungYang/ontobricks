/**
 * SHACL facets of one entity in the Graph Explorer.
 *
 * Every enabled shape targeting the entity's class, one of its ancestors, or
 * no class is checked against that entity only (``/dtwin/nodes/shacl``). The
 * result opens in a popup, summarises in the node detail panel, and rings the
 * node on the canvas.
 *
 * Results are cached per entity for the lifetime of the page and only
 * recomputed on an explicit Re-check. Each completed check is broadcast as a
 * ``shacl-facets:updated`` event; the panel and the canvas listen for it.
 */

// { "<entityUri>": { data: <response>, checkedAt: Date } }
const _shaclCache = {};
let _shaclCurrentUri = null;
let _shaclFilter = 'all';

const SHACL_STATUS = {
    fail:           { icon: 'bi-x-circle-fill',             label: 'failed',         tone: 'danger' },
    error:          { icon: 'bi-exclamation-triangle-fill', label: 'error',          tone: 'warning' },
    not_evaluated:  { icon: 'bi-question-circle',           label: 'not evaluated',  tone: 'draft' },
    not_applicable: { icon: 'bi-dash-circle',               label: 'not applicable', tone: 'info' },
    pass:           { icon: 'bi-check-circle-fill',         label: 'passed',         tone: 'success' }
};

const SHACL_CATEGORIES = [
    { id: 'completeness', label: 'Completeness', icon: 'bi-check2-all' },
    { id: 'cardinality',  label: 'Cardinality',  icon: 'bi-123' },
    { id: 'uniqueness',   label: 'Uniqueness',   icon: 'bi-fingerprint' },
    { id: 'consistency',  label: 'Consistency',  icon: 'bi-link-45deg' },
    { id: 'conformance',  label: 'Conformance',  icon: 'bi-regex' },
    { id: 'structural',   label: 'Structural',   icon: 'bi-diagram-3' }
];

function _shaclEsc(value) {
    if (typeof escapeHtml === 'function') return escapeHtml(value == null ? '' : String(value));
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * Number of shapes the server would report for *className*: enabled shapes
 * on the class, on any ancestor (``parent`` chain), or on no class at all.
 */
function shaclShapeCountFor(className, classes, shapes) {
    if (!className || !Array.isArray(shapes) || !shapes.length) return 0;
    const byName = {};
    (classes || []).forEach(function (c) { if (c && c.name) byName[c.name] = c; });

    const names = new Set();
    const uris = new Set();
    let current = byName[className];
    let name = className;
    while (name && !names.has(name.toLowerCase())) {
        names.add(name.toLowerCase());
        if (current && current.uri) uris.add(current.uri);
        name = current ? (current.parent || '') : '';
        current = byName[name];
    }

    return shapes.filter(function (s) {
        return s && s.enabled !== false;
    }).filter(function (s) {
        const uri = s.target_class_uri || '';
        const target = (s.target_class || '').toLowerCase();
        if (!uri && !target) return true;
        return uris.has(uri) || names.has(target);
    }).length;
}

/**
 * Ring status of a checked node: 'danger' when a Violation failed, 'warning'
 * when a Warning/Info failed or a shape errored, 'success' when every shape
 * passed or did not apply. ``null`` (no ring) when unchecked, empty, or when
 * some shape could not be evaluated without any failure.
 */
function getShaclFacetStatus(entityUri) {
    const entry = _shaclCache[entityUri];
    const facets = entry && entry.data && entry.data.facets;
    if (!facets || !facets.length) return null;
    let warning = false;
    let unchecked = false;
    for (const f of facets) {
        if (f.status === 'fail' && (f.severity || 'sh:Violation') === 'sh:Violation') return 'danger';
        if (f.status === 'fail' || f.status === 'error') warning = true;
        else if (f.status === 'not_evaluated') unchecked = true;
    }
    if (warning) return 'warning';
    return unchecked ? null : 'success';
}

const _shaclColorCache = {};

/** Colour of a ring status, read from the shared ``--db-status-*`` tokens. */
function shaclStatusColor(status) {
    if (!_shaclColorCache[status]) {
        const token = { danger: '--db-status-danger', warning: '--db-status-warning', success: '--db-status-success' }[status];
        const value = token
            ? getComputedStyle(document.documentElement).getPropertyValue(token).trim()
            : '';
        _shaclColorCache[status] = value || 'gray';
    }
    return _shaclColorCache[status];
}

/**
 * Run the facet check for *entityUri*, cache it and broadcast it.
 *
 * @returns {Promise<object|null>} The response, or null when it failed.
 */
async function checkShaclFacets(entityUri) {
    try {
        const resp = await fetch('/dtwin/nodes/shacl?entity_uri=' + encodeURIComponent(entityUri),
            { credentials: 'same-origin' });
        const data = await resp.json();
        if (!resp.ok || !data.success) {
            throw new Error(data.message || data.detail || ('HTTP ' + resp.status));
        }
        _shaclCache[entityUri] = { data: data, checkedAt: new Date() };
        document.dispatchEvent(new CustomEvent('shacl-facets:updated', { detail: { entityUri: entityUri } }));
        return data;
    } catch (err) {
        console.error('[SHACL facets] Check failed:', err);
        if (typeof showNotification === 'function') {
            showNotification('Could not check the SHACL facets: ' + err.message, 'error', 5000);
        }
        return null;
    }
}

// ---------------------------------------------------------------------------
// Popup
// ---------------------------------------------------------------------------

/**
 * Open the facet popup for *entityUri*. A cached result shows immediately;
 * otherwise (or with ``recheck``) the check runs first.
 */
async function openShaclFacetsModal(entityUri, recheck) {
    if (!entityUri) return;
    const modalEl = document.getElementById('shaclFacetsModal');
    if (!modalEl || typeof bootstrap === 'undefined') return;
    if (_shaclCurrentUri !== entityUri) _shaclFilter = 'all';
    _shaclCurrentUri = entityUri;
    bootstrap.Modal.getOrCreateInstance(modalEl).show();

    if (_shaclCache[entityUri] && !recheck) {
        _shaclRenderModal(entityUri);
        return;
    }
    _shaclRenderLoading(entityUri);
    await checkShaclFacets(entityUri);
    if (_shaclCurrentUri === entityUri) _shaclRenderModal(entityUri);
}

function _shaclRenderLoading(entityUri) {
    _shaclSetText('shaclFacetsEntity', _shaclLocalName(entityUri));
    _shaclSetText('shaclFacetsSubtitle', entityUri);
    _shaclSetText('shaclFacetsCheckedAt', '');
    const summary = document.getElementById('shaclFacetsSummary');
    if (summary) summary.innerHTML = '';
    const body = document.getElementById('shaclFacetsBody');
    if (body) {
        body.innerHTML = '<div class="text-center text-muted py-4">' +
            '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>' +
            'Checking SHACL facets…</div>';
    }
    const btn = document.getElementById('shaclFacetsRecheck');
    if (btn) btn.disabled = true;
}

function _shaclRenderModal(entityUri) {
    const entry = _shaclCache[entityUri];
    const btn = document.getElementById('shaclFacetsRecheck');
    if (btn) btn.disabled = false;
    const body = document.getElementById('shaclFacetsBody');
    if (!body) return;
    if (!entry) {
        body.innerHTML = '<p class="text-muted small mb-0">The check did not complete. Use Re-check to try again.</p>';
        return;
    }

    const data = entry.data;
    const classNames = (data.classes || []).map(function (c) { return c.name; }).filter(Boolean);
    _shaclSetText('shaclFacetsEntity', data.entity_label || data.entity_local_id || _shaclLocalName(entityUri));
    const lineage = data.lineage || [];
    _shaclSetText('shaclFacetsSubtitle',
        (lineage.length ? lineage.join(' → ') : classNames.join(', ')) + ' · ' + entityUri);
    _shaclSetText('shaclFacetsCheckedAt', 'Checked at ' + entry.checkedAt.toLocaleTimeString());

    const summary = document.getElementById('shaclFacetsSummary');
    const facets = data.facets || [];
    if (!facets.length) {
        if (summary) summary.innerHTML = '';
        body.innerHTML = '<div class="text-center text-muted py-4">' +
            '<i class="bi bi-shield fs-3 d-block mb-2" aria-hidden="true"></i>' +
            'No SHACL shape applies to ' + _shaclEsc(classNames.join(', ') || 'this entity') + '.</div>';
        return;
    }

    if (summary) summary.innerHTML = _shaclChipsHtml(data.summary || {}, facets.length);
    body.innerHTML = _shaclCategoriesHtml(facets);
    _shaclApplyFilter();
}

function _shaclChipsHtml(summary, total) {
    let html = '<button type="button" class="shacl-chip" data-shacl-filter="all" aria-pressed="false">' +
        '<i class="bi bi-list-ul" aria-hidden="true"></i> All <span class="shacl-chip-count">' + total + '</span></button>';
    Object.keys(SHACL_STATUS).forEach(function (status) {
        const count = summary[status] || 0;
        if (!count) return;
        const meta = SHACL_STATUS[status];
        html += '<button type="button" class="shacl-chip shacl-tone-' + meta.tone + '" data-shacl-filter="' + status +
            '" aria-pressed="false"><i class="bi ' + meta.icon + '" aria-hidden="true"></i> ' + meta.label +
            ' <span class="shacl-chip-count">' + count + '</span></button>';
    });
    return html;
}

function _shaclCategoriesHtml(facets) {
    const known = SHACL_CATEGORIES.map(function (c) { return c.id; });
    const groups = SHACL_CATEGORIES.concat(
        Array.from(new Set(facets.map(function (f) { return f.category || 'other'; })))
            .filter(function (id) { return known.indexOf(id) === -1; })
            .map(function (id) { return { id: id, label: id.charAt(0).toUpperCase() + id.slice(1), icon: 'bi-shield' }; })
    );

    let html = '<div class="accordion shacl-facets-accordion" id="shaclFacetsAccordion">';
    groups.forEach(function (cat, idx) {
        const rows = facets.filter(function (f) { return (f.category || 'other') === cat.id; });
        if (!rows.length) return;
        const failed = rows.filter(function (f) { return f.status === 'fail'; }).length;
        const collapseId = 'shaclCat' + idx;
        html += '<div class="accordion-item" data-shacl-category="' + _shaclEsc(cat.id) + '">' +
            '<h2 class="accordion-header"><button class="accordion-button py-2" type="button" ' +
            'data-bs-toggle="collapse" data-bs-target="#' + collapseId + '" aria-expanded="true" aria-controls="' + collapseId + '">' +
            '<i class="bi ' + cat.icon + ' me-2" aria-hidden="true"></i>' + _shaclEsc(cat.label) +
            '<span class="ms-2 small text-muted">' + rows.length + (failed ? ' · ' + failed + ' failed' : '') + '</span>' +
            '</button></h2>' +
            '<div id="' + collapseId + '" class="accordion-collapse collapse show">' +
            '<div class="accordion-body p-0"><ul class="list-group list-group-flush">' +
            rows.map(_shaclFacetRowHtml).join('') +
            '</ul></div></div></div>';
    });
    return html + '</div>';
}

function _shaclFacetRowHtml(f) {
    const meta = SHACL_STATUS[f.status] || SHACL_STATUS.not_evaluated;
    const severity = (f.severity || 'sh:Violation').replace(/^sh:/, '');
    let html = '<li class="list-group-item shacl-facet" data-shacl-status="' + _shaclEsc(f.status) + '">' +
        '<div class="d-flex align-items-start gap-2">' +
        '<span class="shacl-status shacl-tone-' + meta.tone + '" title="' + meta.label + '">' +
        '<i class="bi ' + meta.icon + '" aria-hidden="true"></i>' +
        '<span class="visually-hidden">' + meta.label + '</span></span>' +
        '<div class="flex-grow-1 shacl-facet-main">' +
        '<div class="fw-semibold">' + _shaclEsc(f.label || f.shape_id) +
        (f.status === 'fail' ? ' <span class="badge shacl-severity shacl-severity-' + severity.toLowerCase() + '">' + _shaclEsc(severity) + '</span>' : '') +
        '</div>';

    const target = [f.property_path, f.constraint].filter(Boolean).join(' · ');
    if (target) html += '<div class="small text-muted"><code>' + _shaclEsc(target) + '</code></div>';
    if (f.inherited_from) {
        html += '<div class="small text-muted"><i class="bi bi-diagram-2 me-1" aria-hidden="true"></i>inherited from ' +
            _shaclEsc(f.inherited_from) + '</div>';
    }
    if ((f.status === 'fail' || f.status === 'pass') && f.property_path) {
        const values = f.values || [];
        const found = values.length
            ? values.map(function (v) { return '<code>' + _shaclEsc(v) + '</code>'; }).join(', ')
            : '<span class="text-muted">(none)</span>';
        html += '<div class="small">Found: ' + found +
            (f.values_truncated ? ' <span class="text-muted">+ more values not shown</span>' : '') + '</div>';
    }
    if (f.message && f.status !== 'pass') {
        html += '<div class="small shacl-facet-message">' + _shaclEsc(f.message) + '</div>';
    }
    return html + '</div></div></li>';
}

function _shaclApplyFilter() {
    document.querySelectorAll('#shaclFacetsSummary [data-shacl-filter]').forEach(function (chip) {
        chip.setAttribute('aria-pressed', chip.getAttribute('data-shacl-filter') === _shaclFilter ? 'true' : 'false');
    });
    document.querySelectorAll('#shaclFacetsBody .shacl-facet').forEach(function (row) {
        const show = _shaclFilter === 'all' || row.getAttribute('data-shacl-status') === _shaclFilter;
        row.classList.toggle('d-none', !show);
    });
    document.querySelectorAll('#shaclFacetsBody [data-shacl-category]').forEach(function (item) {
        item.classList.toggle('d-none', !item.querySelector('.shacl-facet:not(.d-none)'));
    });
}

// ---------------------------------------------------------------------------
// Detail panel
// ---------------------------------------------------------------------------

/**
 * Render the Data quality section body of the node detail panel: the shape
 * count and a Check button, then — once checked — the summary with Details
 * and Re-check.
 */
function renderShaclFacetSection(entityUri, shapeCount) {
    return '<div class="shacl-panel" data-shacl-panel="' + _shaclEsc(entityUri) + '" data-shacl-count="' +
        (shapeCount || 0) + '">' + _shaclPanelInner(entityUri, shapeCount) + '</div>';
}

function _shaclPanelInner(entityUri, shapeCount) {
    const entry = _shaclCache[entityUri];
    const safeUri = _shaclEsc(entityUri);
    if (!entry) {
        return '<p class="small text-muted mb-2">' +
            (shapeCount === 1 ? '1 SHACL shape applies' : shapeCount + ' SHACL shapes apply') +
            ' to this entity.</p>' +
            '<button type="button" class="btn btn-sm btn-outline-primary w-100" data-shacl-action="check" data-uri="' + safeUri + '">' +
            '<i class="bi bi-shield-check me-1" aria-hidden="true"></i>Check</button>';
    }
    const s = entry.data.summary || {};
    let chips = '';
    Object.keys(SHACL_STATUS).forEach(function (status) {
        if (!s[status]) return;
        const meta = SHACL_STATUS[status];
        chips += '<span class="shacl-chip shacl-chip--static shacl-tone-' + meta.tone + '">' +
            '<i class="bi ' + meta.icon + '" aria-hidden="true"></i> ' + s[status] + ' ' + meta.label + '</span>';
    });
    return '<div class="shacl-facets-summary mb-2">' + (chips || '<span class="small text-muted">No SHACL shape applies</span>') + '</div>' +
        '<div class="small text-muted mb-2">Checked at ' + entry.checkedAt.toLocaleTimeString() + '</div>' +
        '<div class="d-flex gap-1">' +
        '<button type="button" class="btn btn-sm btn-outline-primary flex-grow-1" data-shacl-action="details" data-uri="' + safeUri + '">' +
        '<i class="bi bi-list-check me-1" aria-hidden="true"></i>Details</button>' +
        '<button type="button" class="btn btn-sm btn-outline-secondary" data-shacl-action="recheck" data-uri="' + safeUri + '">' +
        '<i class="bi bi-arrow-repeat me-1" aria-hidden="true"></i>Re-check</button></div>';
}

function _shaclRefreshPanels(entityUri) {
    document.querySelectorAll('[data-shacl-panel]').forEach(function (panel) {
        if (panel.getAttribute('data-shacl-panel') !== entityUri) return;
        panel.innerHTML = _shaclPanelInner(entityUri, parseInt(panel.getAttribute('data-shacl-count'), 10) || 0);
    });
}

// ---------------------------------------------------------------------------
// Helpers and wiring
// ---------------------------------------------------------------------------

function _shaclSetText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text || '';
}

function _shaclLocalName(uri) {
    return String(uri || '').split('#').pop().split('/').pop();
}

document.addEventListener('click', function (e) {
    const chip = e.target.closest('#shaclFacetsSummary [data-shacl-filter]');
    if (chip) {
        const value = chip.getAttribute('data-shacl-filter');
        _shaclFilter = (value === _shaclFilter) ? 'all' : value;
        _shaclApplyFilter();
        return;
    }
    if (e.target.closest('#shaclFacetsRecheck')) {
        if (_shaclCurrentUri) openShaclFacetsModal(_shaclCurrentUri, true);
        return;
    }
    if (e.target.closest('#shaclFacetsOpenDq')) {
        const modalEl = document.getElementById('shaclFacetsModal');
        if (modalEl) bootstrap.Modal.getOrCreateInstance(modalEl).hide();
        const link = document.querySelector('[data-section="dataquality"]');
        if (link) link.click();
        return;
    }
    const action = e.target.closest('[data-shacl-action]');
    if (!action) return;
    const uri = action.getAttribute('data-uri');
    const kind = action.getAttribute('data-shacl-action');
    if (kind === 'details') {
        openShaclFacetsModal(uri);
    } else {
        action.disabled = true;
        action.innerHTML = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Checking…';
        checkShaclFacets(uri).then(function (data) {
            if (!data) _shaclRefreshPanels(uri);
        });
    }
});

document.addEventListener('shacl-facets:updated', function (e) {
    _shaclRefreshPanels(e.detail && e.detail.entityUri);
});

window.openShaclFacetsModal = openShaclFacetsModal;
window.renderShaclFacetSection = renderShaclFacetSection;
window.getShaclFacetStatus = getShaclFacetStatus;
window.shaclStatusColor = shaclStatusColor;
window.shaclShapeCountFor = shaclShapeCountFor;

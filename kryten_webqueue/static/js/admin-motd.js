/* Admin MOTD panel — inspect the resolved weekend grid, override slots, publish. */

let GRID = null;

function weekParam() {
    return document.getElementById('week-select').value;
}

function slotBadge(slot) {
    if (slot.source === 'override') return '<span class="badge badge-accent">override</span>';
    if (slot.source === 'omdb') return '<span class="badge">resolved</span>';
    return '<span class="badge badge-warn">mystery</span>';
}

async function loadGrid() {
    const status = document.getElementById('grid-status');
    const grid = document.getElementById('motd-grid');
    status.textContent = 'Loading…';
    grid.innerHTML = '';

    const resp = await fetch(`/admin/motd/grid?week=${encodeURIComponent(weekParam())}`);
    if (!resp.ok) {
        status.innerHTML = '<p class="empty-state">Failed to load the grid.</p>';
        return;
    }
    GRID = await resp.json();

    document.getElementById('current-motd').textContent =
        GRID.current_motd || '(api-gate unreachable or MOTD empty)';

    const resolved = GRID.slots.filter(s => s.resolved).length;
    const warn = (GRID.warnings || []).length
        ? `<div class="badge badge-warn">${escapeHtml(GRID.warnings.join('; '))}</div>`
        : '';
    status.innerHTML = `Sheet <strong>${escapeHtml(GRID.week_key)}</strong> —
        ${resolved}/${GRID.slots.length} slots resolved. ${warn}`;

    // Group by night so the grid reads the way it airs.
    const nights = [];
    for (const slot of GRID.slots) {
        if (!nights.length || nights[nights.length - 1].night !== slot.night) {
            nights.push({ night: slot.night, label: slot.night_label, slots: [] });
        }
        nights[nights.length - 1].slots.push(slot);
    }

    grid.innerHTML = nights.map(n => `
        <h3 class="motd-night">${escapeHtml(n.label)}</h3>
        <div class="motd-slots">
            ${n.slots.map(s => `
                <div class="motd-slot ${s.resolved ? '' : 'motd-slot-mystery'}">
                    <img src="${escapeHtml(s.poster_url)}" alt="" loading="lazy">
                    <div class="motd-slot-meta">
                        <div class="motd-slot-title">${escapeHtml(s.title || '— empty slot —')}</div>
                        <div class="muted">${slotBadge(s)} ${s.note ? escapeHtml(s.note) : ''}</div>
                        ${s.imdb_id ? `<a class="muted" href="https://www.imdb.com/title/${escapeHtml(s.imdb_id)}/" target="_blank" rel="noopener">${escapeHtml(s.imdb_id)}</a>` : ''}
                    </div>
                    <button type="button" class="btn btn-sm" data-edit="${escapeHtml(s.slot_key)}">Override</button>
                </div>`).join('')}
        </div>`).join('');

    grid.querySelectorAll('[data-edit]').forEach(btn => {
        btn.addEventListener('click', () => openOverride(btn.getAttribute('data-edit')));
    });
}

// ---------- override editor ----------

function openOverride(slotKey) {
    const slot = GRID.slots.find(s => s.slot_key === slotKey);
    if (!slot) return;

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-box" role="dialog" aria-modal="true">
            <h3>Override ${escapeHtml(slot.night_label)} · slot ${slot.position}</h3>
            <div class="modal-body">
                <div class="field">
                    <label for="ov-title">Title</label>
                    <input type="text" id="ov-title" placeholder="Title (YYYY)"
                           value="${escapeHtml(slot.title || '')}">
                    <span class="field-help">A clean title with a year resolves art and the IMDb link automatically.</span>
                </div>
                <div class="field">
                    <label for="ov-href">Link</label>
                    <input type="url" id="ov-href" placeholder="https://… (blank = IMDb)">
                    <span class="field-help">Only for the rare case the poster shouldn't point at IMDb.</span>
                </div>
                <div class="field">
                    <label for="ov-poster">Poster image URL</label>
                    <input type="url" id="ov-poster" placeholder="https://…">
                </div>
                <div class="field">
                    <label for="ov-file">…or upload alternate art</label>
                    <input type="file" id="ov-file" accept="image/jpeg,image/png,image/webp,image/gif">
                    <span class="field-help">JPEG, PNG, WebP, or GIF. An upload wins over the URL above.</span>
                </div>
            </div>
            <div class="modal-actions">
                <button class="btn btn-danger" data-action="clear">Clear Override</button>
                <button class="btn btn-secondary" data-action="cancel">Cancel</button>
                <button class="btn btn-primary" data-action="save">Save</button>
            </div>
        </div>`;

    const close = () => overlay.remove();
    overlay.addEventListener('click', async (e) => {
        if (e.target === overlay) return close();
        const action = e.target.getAttribute('data-action');
        if (action === 'cancel') return close();
        if (action === 'clear') {
            await clearOverride(slotKey);
            close();
        }
        if (action === 'save') {
            await saveOverride(slotKey, overlay);
            close();
        }
    });
    document.body.appendChild(overlay);
}

async function saveOverride(slotKey, overlay) {
    const week = weekParam();
    const title = overlay.querySelector('#ov-title').value.trim();
    const href = overlay.querySelector('#ov-href').value.trim();
    const posterUrl = overlay.querySelector('#ov-poster').value.trim();
    const fileInput = overlay.querySelector('#ov-file');
    const file = fileInput.files && fileInput.files[0];

    let resp;
    if (file) {
        const form = new FormData();
        form.append('file', file);
        form.append('week', week);
        if (title) form.append('title', title);
        if (href) form.append('href', href);
        resp = await fetch(`/admin/motd/overrides/${encodeURIComponent(slotKey)}/art`, {
            method: 'POST', body: form,
        });
    } else {
        resp = await fetch(
            `/admin/motd/overrides/${encodeURIComponent(slotKey)}?week=${encodeURIComponent(week)}`,
            {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    title: title || null,
                    href: href || null,
                    poster_url: posterUrl || null,
                }),
            });
    }

    if (resp.ok) {
        showToast('Override saved', 'success');
        loadGrid();
    } else {
        const data = await resp.json().catch(() => ({}));
        showToast(data.detail || 'Save failed', 'error');
    }
}

async function clearOverride(slotKey) {
    const resp = await fetch(
        `/admin/motd/overrides/${encodeURIComponent(slotKey)}?week=${encodeURIComponent(weekParam())}`,
        { method: 'DELETE' });
    if (resp.ok) {
        showToast('Override cleared', 'success');
        loadGrid();
    } else {
        const data = await resp.json().catch(() => ({}));
        showToast(data.detail || 'Nothing to clear', 'error');
    }
}

// ---------- preview / publish ----------

async function previewHtml() {
    const resp = await fetch(`/admin/motd/render?week=${encodeURIComponent(weekParam())}`);
    if (!resp.ok) { showToast('Preview failed', 'error'); return; }
    const { html } = await resp.json();
    document.getElementById('preview-html').textContent = html;
    document.getElementById('preview-section').hidden = false;
}

async function publish() {
    if (!confirm('Replace the live channel MOTD with this grid?')) return;
    const resp = await fetch(`/admin/motd/publish?week=${encodeURIComponent(weekParam())}`, {
        method: 'POST',
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) { showToast(data.detail || 'Publish failed', 'error'); return; }
    if (data.started) {
        showToast('Publishing… art downloads may take a minute.', 'success');
        // The job downloads art, so give it a beat before re-reading the live MOTD.
        setTimeout(loadGrid, 8000);
    } else {
        showToast(data.reason || 'A MOTD publish is already running', 'error');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('btn-reload').addEventListener('click', loadGrid);
    document.getElementById('btn-preview').addEventListener('click', previewHtml);
    document.getElementById('btn-publish').addEventListener('click', publish);
    document.getElementById('week-select').addEventListener('change', loadGrid);
    document.getElementById('btn-copy').addEventListener('click', () => {
        navigator.clipboard.writeText(document.getElementById('preview-html').textContent)
            .then(() => showToast('Copied', 'success'));
    });
    loadGrid();
});

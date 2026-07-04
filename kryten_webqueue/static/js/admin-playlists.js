let editorId = null;
let editorItems = [];   // working list: {media_type, media_id, title, duration_sec}
let editorImmutable = false;

// ---------- LIST ----------
async function loadPlaylists() {
    const resp = await fetch('/admin/playlists/');
    const el = document.getElementById('playlists-list');
    if (!resp.ok) { el.innerHTML = '<p class="empty-state">Failed to load.</p>'; return; }
    const rows = await resp.json();
    if (!rows.length) { el.innerHTML = '<p class="empty-state">No playlists yet.</p>'; return; }
    el.innerHTML = `<table class="admin-table">
        <tr><th>Name</th><th>Reserved</th><th>Created by</th><th></th></tr>
        ${rows.map(p => `
            <tr>
                <td><a href="#" onclick="openEditor(${p.id});return false;">${escapeHtml(p.name)}</a>
                    ${p.description ? `<div class="muted">${escapeHtml(p.description)}</div>` : ''}</td>
                <td>${p.promo_type
                    ? `<span class="badge badge-accent" title="Promo pool">Promo: ${escapeHtml(p.promo_type)}</span>`
                    : (p.is_immutable ? '<span class="badge badge-warn">Non-preemptable</span>' : '<span class="muted">Preemptable</span>')}</td>
                <td>${escapeHtml(p.created_by || '')}</td>
                <td class="row-actions">
                    <button class="btn btn-sm" onclick="toggleImmutable(${p.id}, ${p.is_immutable ? 1 : 0}, '${escapeHtml(p.name)}')"
                        title="${p.is_immutable ? 'Release items back to the public catalog' : 'Reserve items — hide from public catalog/search'}">
                        ${p.is_immutable ? 'Release' : 'Reserve'}</button>
                    <button class="btn btn-sm" onclick="openEditor(${p.id})">Edit</button>
                    <button class="btn btn-sm btn-danger" onclick="deletePlaylist(${p.id}, '${escapeHtml(p.name)}')">Delete</button>
                </td>
            </tr>`).join('')}
    </table>`;
}

async function toggleImmutable(id, currentlyImmutable, name) {
    const makeImmutable = !currentlyImmutable;
    const verb = makeImmutable ? 'Reserve' : 'Release';
    if (!confirm(`${verb} "${name}"?\n\n${makeImmutable
        ? 'Its items will be hidden from the public catalog and search and reserved for scheduled play.'
        : 'Its items will return to the public catalog and become available for pay-to-play.'}`)) return;
    const resp = await fetch(`/admin/playlists/${id}`, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({is_immutable: makeImmutable})
    });
    showToast(resp.ok ? `${verb}d` : `${verb} failed`, resp.ok ? 'success' : 'error');
    loadPlaylists();
}

function showCreateModal() {
    showModal(`
        <h3>New Playlist</h3>
        <label class="field"><span>Name</span><input type="text" id="pl-name"></label>
        <label class="field"><span>Description</span><input type="text" id="pl-desc"></label>
        <label class="check"><input type="checkbox" id="pl-immut"> Non-preemptable (reserve items — hidden from public catalog)</label>
        <div class="modal-actions">
            <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
            <button class="btn btn-primary" onclick="createPlaylist()">Create</button>
        </div>`);
}

async function createPlaylist() {
    const name = document.getElementById('pl-name').value.trim();
    if (!name) { showToast('Name required', 'error'); return; }
    const body = {
        name,
        description: document.getElementById('pl-desc').value.trim() || null,
        is_immutable: document.getElementById('pl-immut').checked,
    };
    const resp = await fetch('/admin/playlists/', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    closeModal();
    if (resp.ok) { const d = await resp.json(); showToast('Created'); loadPlaylists(); openEditor(d.id); }
    else showToast('Create failed', 'error');
}

async function deletePlaylist(id, name) {
    if (!confirm(`Delete playlist "${name}"? This cannot be undone.`)) return;
    const resp = await fetch(`/admin/playlists/${id}`, {method: 'DELETE'});
    showToast(resp.ok ? 'Deleted' : 'Delete failed', resp.ok ? 'success' : 'error');
    loadPlaylists();
}

// ---------- EDITOR ----------
async function openEditor(id) {
    const resp = await fetch(`/admin/playlists/${id}`);
    if (!resp.ok) { showToast('Load failed', 'error'); return; }
    const pl = await resp.json();
    editorId = id;
    editorImmutable = !!pl.is_immutable;
    editorItems = (pl.items || []).map(i => ({
        media_type: i.media_type, media_id: i.media_id, title: i.title, duration_sec: i.duration_sec
    }));
    document.getElementById('editor-title').textContent = pl.name;
    document.getElementById('editor-meta').textContent =
        `${pl.is_immutable ? 'Non-preemptable (reserved) · ' : ''}${pl.description || ''}`;
    document.getElementById('list-view').classList.add('hidden');
    document.getElementById('editor-view').classList.remove('hidden');
    document.getElementById('cat-results').innerHTML = '';
    document.getElementById('import-errors').innerHTML = '';
    renderEditorItems();
}

function closeEditor() {
    document.getElementById('editor-view').classList.add('hidden');
    document.getElementById('list-view').classList.remove('hidden');
    editorId = null; editorItems = [];
    loadPlaylists();
}

function renderEditorItems() {
    document.getElementById('item-count').textContent = editorItems.length;
    const el = document.getElementById('editor-items');
    if (!editorItems.length) { el.innerHTML = '<li class="muted">No items. Add from the catalog or text import.</li>'; return; }
    el.innerHTML = editorItems.map((it, i) => `
        <li class="editor-row" draggable="true" data-i="${i}"
            ondragstart="dragStart(event,${i})" ondragover="event.preventDefault()" ondrop="dropOn(event,${i})">
            <span class="drag-handle" title="Drag to reorder">⠿</span>
            <span class="pos">${i + 1}</span>
            <span class="ed-title">${escapeHtml(it.title || it.media_id)}</span>
            <span class="ed-type">${escapeHtml(it.media_type)}</span>
            <span class="ed-dur">${it.duration_sec ? fmtDur(it.duration_sec) : '—'}</span>
            <span class="ed-move">
                <button class="btn btn-xs" onclick="moveItem(${i},-1)" ${i===0?'disabled':''}>↑</button>
                <button class="btn btn-xs" onclick="moveItem(${i},1)" ${i===editorItems.length-1?'disabled':''}>↓</button>
                <button class="btn btn-xs btn-danger" onclick="removeItem(${i})">✕</button>
            </span>
        </li>`).join('');
}

function moveItem(i, dir) {
    const j = i + dir;
    if (j < 0 || j >= editorItems.length) return;
    [editorItems[i], editorItems[j]] = [editorItems[j], editorItems[i]];
    renderEditorItems();
}
function removeItem(i) { editorItems.splice(i, 1); renderEditorItems(); }

let dragSrc = null;
function dragStart(e, i) { dragSrc = i; e.dataTransfer.effectAllowed = 'move'; }
function dropOn(e, i) {
    e.preventDefault();
    if (dragSrc === null || dragSrc === i) return;
    const [moved] = editorItems.splice(dragSrc, 1);
    editorItems.splice(i, 0, moved);
    dragSrc = null;
    renderEditorItems();
}

async function catalogSearch() {
    const q = document.getElementById('cat-search').value.trim();
    if (!q) return;
    const resp = await fetch(`/catalog/search?q=${encodeURIComponent(q)}`);
    const el = document.getElementById('cat-results');
    if (!resp.ok) { el.innerHTML = '<p class="muted">Search failed.</p>'; return; }
    const data = await resp.json();
    const items = data.items || [];
    if (!items.length) { el.innerHTML = '<p class="muted">No results.</p>'; return; }
    el.innerHTML = items.map(it => `
        <div class="cat-result">
            <span class="ed-title">${escapeHtml(it.title)}</span>
            <span class="ed-dur">${it.duration_sec ? fmtDur(it.duration_sec) : ''}</span>
            <button class="btn btn-xs btn-primary"
                onclick='addCatalogItem(${JSON.stringify(it).replace(/'/g, "&#39;")})'>Add</button>
        </div>`).join('');
}

function addCatalogItem(it) {
    editorItems.push({
        media_type: 'cm',
        media_id: it.manifest_url || it.friendly_token,
        title: it.title,
        duration_sec: it.duration_sec,
    });
    renderEditorItems();
    showToast('Added');
}

async function parseImport() {
    const text = document.getElementById('import-text').value;
    if (!text.trim()) return;
    const resp = await fetch('/admin/playlists/parse-text', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})
    });
    if (!resp.ok) { showToast('Parse failed', 'error'); return; }
    const data = await resp.json();
    (data.items || []).forEach(it => editorItems.push(it));
    renderEditorItems();
    const errEl = document.getElementById('import-errors');
    if ((data.errors || []).length) {
        errEl.innerHTML = `<p class="muted">${data.errors.length} unresolved:</p><ul>` +
            data.errors.map(e => `<li>Line ${e.line}: <code>${escapeHtml(e.token)}</code> (${e.reason})</li>`).join('') + '</ul>';
    } else { errEl.innerHTML = ''; }
    showToast(`Appended ${(data.items || []).length} item(s)`);
    document.getElementById('import-text').value = '';
}

function loadImportFile(event) {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
        const ta = document.getElementById('import-text');
        // Append (with a newline separator) so the admin can review before parsing.
        ta.value = (ta.value.trim() ? ta.value.replace(/\s*$/, '') + '\n' : '') + reader.result;
        showToast(`Loaded ${file.name}`);
    };
    reader.onerror = () => showToast('Could not read file', 'error');
    reader.readAsText(file);
    // Reset so picking the same file again re-fires change.
    event.target.value = '';
}

async function saveItems() {
    if (editorId === null) return;
    const resp = await fetch(`/admin/playlists/${editorId}/items`, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({items: editorItems.map((it, i) => ({...it, position: i}))})
    });
    showToast(resp.ok ? 'Saved' : 'Save failed', resp.ok ? 'success' : 'error');
}

async function importToLive() {
    if (editorId === null) return;
    if (!confirm('Load this playlist into the live CyTube queue now?')) return;
    const resp = await fetch(`/admin/playlists/${editorId}/import`, {method: 'POST'});
    const data = await resp.json().catch(() => ({}));
    if (resp.ok && data.success) showToast(`Imported ${data.added} item(s)${data.errors ? `, ${data.errors} errors` : ''}`);
    else showToast(data.error || 'Import failed', 'error');
}

function editMeta() {
    showModal(`
        <h3>Rename Playlist</h3>
        <label class="field"><span>Name</span><input type="text" id="em-name" value="${escapeHtml(document.getElementById('editor-title').textContent)}"></label>
        <label class="check"><input type="checkbox" id="em-immut" ${editorImmutable ? 'checked' : ''}> Non-preemptable (reserve items)</label>
        <div class="modal-actions">
            <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
            <button class="btn btn-primary" onclick="saveMeta()">Save</button>
        </div>`);
}

async function saveMeta() {
    const name = document.getElementById('em-name').value.trim();
    const is_immutable = document.getElementById('em-immut').checked;
    if (!name) { showToast('Name required', 'error'); return; }
    const resp = await fetch(`/admin/playlists/${editorId}`, {
        method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, is_immutable})
    });
    closeModal();
    if (resp.ok) { editorImmutable = is_immutable; document.getElementById('editor-title').textContent = name; showToast('Saved'); }
    else showToast('Save failed', 'error');
}

// ---------- generic modal ----------
loadPlaylists();
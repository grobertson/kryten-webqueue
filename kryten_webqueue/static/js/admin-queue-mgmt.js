let refreshTimer = null;

async function loadQueue() {
    const resp = await fetch('/queue/state');
    const el = document.getElementById('queue-table');
    if (!resp.ok) { el.innerHTML = '<p class="empty-state">Failed to load.</p>'; return; }
    const state = await resp.json();
    const np = state.now_playing;
    document.getElementById('np-line').innerHTML = np
        ? `Now playing: <strong>${escapeHtml(np.title || 'Unknown')}</strong>`
        : 'Nothing playing';
    const items = state.items || [];
    if (!items.length) { el.innerHTML = '<p class="empty-state">Queue is empty.</p>'; return; }
    el.innerHTML = `<table class="admin-table">
        <tr><th>#</th><th>Title</th><th>Type</th><th>Pay</th><th>By</th><th>Z</th><th>ETA</th><th></th></tr>
        ${items.map((it, i) => `
            <tr>
                <td>${i + 1}</td>
                <td>${escapeHtml(it.title || it.media_id)}
                    <span class="muted">${it.duration_sec ? fmtDur(it.duration_sec) : ''}</span></td>
                <td>${escapeHtml(it.media_type || '')}</td>
                <td>${it.is_pay ? `<span class="badge badge-accent">${escapeHtml(it.tier || 'pay')}</span>` : (it.schedule_id ? '<span class="badge">sched</span>' : '—')}</td>
                <td>${escapeHtml(it.paid_by || '')}</td>
                <td>${it.z_cost != null ? it.z_cost : '—'}</td>
                <td>${it.estimated_start_at ? formatLocalTime(it.estimated_start_at) : '—'}</td>
                <td class="row-actions">
                    <button class="btn btn-xs" onclick="jumpTo(${it.uid})" title="Play this now">Jump</button>
                    <button class="btn btn-xs btn-danger" onclick="removeItem(${it.uid}, ${it.is_pay ? 1 : 0})">Remove</button>
                </td>
            </tr>`).join('')}
    </table>`;
}

async function jumpTo(uid) {
    if (!confirm('Jump to this item now?')) return;
    const resp = await fetch(`/admin/queue/${uid}/jump`, {method: 'POST'});
    showToast(resp.ok ? 'Jumped' : 'Failed', resp.ok ? 'success' : 'error');
    loadQueue();
}

async function removeItem(uid, isPay) {
    if (!confirm(isPay ? 'Remove this paid item? It will be refunded.' : 'Remove this item?')) return;
    const resp = await fetch(`/admin/queue/${uid}`, {method: 'DELETE'});
    showToast(resp.ok ? 'Removed' : 'Failed', resp.ok ? 'success' : 'error');
    loadQueue();
}

async function clearAll() {
    if (!confirm('Clear the entire queue? All pay items will be refunded.')) return;
    const resp = await fetch('/admin/queue/clear', {method: 'POST'});
    const data = await resp.json().catch(() => ({}));
    showToast(resp.ok ? `Cleared (${data.refunded || 0} refunded)` : 'Failed', resp.ok ? 'success' : 'error');
    loadQueue();
}

// ---- Add item ----
function showAddModal() {
    showModal(`
        <h3>Add Item (Admin)</h3>
        <div class="search-form">
            <input type="text" id="aq-search" placeholder="Search catalog…" onkeydown="if(event.key==='Enter')aqSearch()">
            <button class="btn btn-sm" onclick="aqSearch()">Search</button>
        </div>
        <div class="field">
            <span>Placement</span>
            <select id="aq-mode">
                <option value="after_purchased">After purchased items</option>
                <option value="playnext_refund">Play next (refund displaced pay items)</option>
            </select>
        </div>
        <div id="aq-results" class="cat-results"></div>
        <div class="modal-actions">
            <button class="btn btn-secondary" onclick="closeModal()">Close</button>
        </div>`);
}

async function aqSearch() {
    const q = document.getElementById('aq-search').value.trim();
    if (!q) return;
    const resp = await fetch(`/catalog/search?q=${encodeURIComponent(q)}`);
    const el = document.getElementById('aq-results');
    if (!resp.ok) { el.innerHTML = '<p class="muted">Search failed.</p>'; return; }
    const data = await resp.json();
    const items = data.items || [];
    if (!items.length) { el.innerHTML = '<p class="muted">No results.</p>'; return; }
    el.innerHTML = items.map(it => `
        <div class="cat-result">
            <span class="ed-title">${escapeHtml(it.title)}</span>
            <button class="btn btn-xs btn-primary" onclick="aqAdd('${escapeHtml(it.friendly_token)}')">Add</button>
        </div>`).join('');
}

async function aqAdd(token) {
    const mode = document.getElementById('aq-mode').value;
    const resp = await fetch('/admin/queue/add', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({friendly_token: token, mode})
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) { showToast(`Queued${data.refunded ? ` (${data.refunded} refunded)` : ''}`); loadQueue(); }
    else showToast(data.detail || 'Add failed', 'error');
}

async function syncNow() {
    const resp = await fetch('/admin/queue/sync-now', {method: 'POST'});
    showToast(resp.ok ? 'Sync started' : 'Failed', resp.ok ? 'success' : 'error');
    setTimeout(loadSyncLogs, 1500);
}

async function loadSyncLogs() {
    const resp = await fetch('/admin/queue/sync-logs');
    const el = document.getElementById('sync-logs');
    if (!resp.ok) { el.innerHTML = '<p class="empty-state">Failed.</p>'; return; }
    const logs = await resp.json();
    if (!logs.length) { el.innerHTML = '<p class="empty-state">No sync logs yet.</p>'; return; }
    el.innerHTML = `<table class="admin-table">
        <tr><th>Started</th><th>Status</th><th>New</th><th>Updated</th><th>Errors</th></tr>
        ${logs.slice(0, 8).map(l => `
            <tr>
                <td>${formatLocalDateTime(l.started_at)}</td>
                <td><span class="job-status job-status-${escapeHtml(l.status || '')}">${escapeHtml(l.status || '')}</span></td>
                <td>${l.items_new || 0}</td>
                <td>${l.items_updated || 0}</td>
                <td>${l.errors || 0}</td>
            </tr>`).join('')}
    </table>`;
}

loadQueue();
loadSyncLogs();
refreshTimer = setInterval(loadQueue, 5000);
window.addEventListener('beforeunload', () => clearInterval(refreshTimer));
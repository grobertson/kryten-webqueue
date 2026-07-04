const PROMO_TYPE_LABELS = {
    channel_identity: 'Channel Identity',
    event: 'Event',
    mod_shoutout: 'Mod Shoutout',
    feature_presentation: 'Feature Presentation',
    viewers_choice: "Viewer's Choice",
};

let ALL_PLAYLISTS = [];
let PROMO_META = { promo_types: [], general_types: [], lead_in_types: [] };

function typeLabel(t) { return PROMO_TYPE_LABELS[t] || t; }

// ---------- POOLS ----------
async function loadPools() {
    const [plResp, cfgResp] = await Promise.all([
        fetch('/admin/playlists/'),
        fetch('/admin/promos/config'),
    ]);
    const el = document.getElementById('pools-list');
    if (!plResp.ok || !cfgResp.ok) { el.innerHTML = '<p class="empty-state">Failed to load.</p>'; return; }
    ALL_PLAYLISTS = await plResp.json();
    const cfg = await cfgResp.json();
    PROMO_META = cfg;

    const options = (current) => {
        const opts = ['<option value="">— not a promo —</option>'];
        for (const t of cfg.promo_types) {
            opts.push(`<option value="${t}" ${current === t ? 'selected' : ''}>${escapeHtml(typeLabel(t))}</option>`);
        }
        return opts.join('');
    };

    if (!ALL_PLAYLISTS.length) { el.innerHTML = '<p class="empty-state">No saved playlists yet.</p>'; return; }

    el.innerHTML = `<table class="admin-table">
        <tr><th>Playlist</th><th>Current Role</th><th>Promo Type</th></tr>
        ${ALL_PLAYLISTS.map(p => `
            <tr>
                <td><a href="/admin/playlists">${escapeHtml(p.name)}</a>
                    ${p.description ? `<div class="muted">${escapeHtml(p.description)}</div>` : ''}</td>
                <td>${p.promo_type
                    ? `<span class="badge badge-accent">${escapeHtml(typeLabel(p.promo_type))}</span>`
                    : (p.is_immutable ? '<span class="badge badge-warn">Non-preemptable</span>' : '<span class="muted">Preemptable</span>')}</td>
                <td>
                    <select onchange="setPromoType(${p.id}, this.value, '${escapeHtml(p.name)}')"
                        ${p.is_immutable ? 'disabled title="Make the playlist preemptable first"' : ''}>
                        ${options(p.promo_type || '')}
                    </select>
                </td>
            </tr>`).join('')}
    </table>
    <p class="muted" style="margin-top:0.5rem;font-size:0.8rem;">
        Non-preemptable playlists can't be promo pools — make them preemptable on the Playlists page first.
        Multiple playlists may share a type; their clips are unioned into that type's pool.
    </p>`;
}

async function setPromoType(id, promoType, name) {
    const resp = await fetch(`/admin/playlists/${id}`, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({promo_type: promoType || null})
    });
    if (resp.ok) {
        showToast(promoType ? `"${name}" → ${typeLabel(promoType)}` : `"${name}" is no longer a promo pool`, 'success');
    } else {
        const data = await resp.json().catch(() => ({}));
        showToast(data.detail || 'Update failed', 'error');
    }
    loadPools();
}

// ---------- SETTINGS ----------
let CURRENT_CONFIG = null;

async function loadSettings() {
    const resp = await fetch('/admin/promos/config');
    const el = document.getElementById('settings-view');
    if (!resp.ok) { el.innerHTML = '<p class="empty-state">Failed to load.</p>'; return; }
    const { config: c } = await resp.json();
    CURRENT_CONFIG = c;
    const g = c.general || {};
    const types = c.types || {};

    const typeRows = Object.keys(types).map(t => {
        const tc = types[t] || {};
        const isGeneral = (PROMO_META.general_types || []).includes(t);
        return `<tr data-type="${escapeHtml(t)}">
            <td>${escapeHtml(typeLabel(t))}</td>
            <td><input type="checkbox" data-field="enabled" ${tc.enabled ? 'checked' : ''}></td>
            <td>
                <select data-field="order">
                    <option value="random" ${tc.order === 'random' ? 'selected' : ''}>random</option>
                    <option value="sequential" ${tc.order === 'sequential' ? 'selected' : ''}>sequential</option>
                </select>
            </td>
            <td>${isGeneral
                ? `<input type="number" min="0" step="1" data-field="weight" value="${escapeHtml(String(tc.weight ?? 1))}" style="width:5rem;">`
                : '<span class="muted">n/a</span>'}</td>
        </tr>`;
    }).join('');

    el.innerHTML = `
        <div class="settings-grid">
            <label><span class="muted">System</span>
                <input type="checkbox" id="cfg-enabled" ${c.enabled ? 'checked' : ''}> Enabled</label>
            <label><span class="muted">Movie threshold (min)</span>
                <input type="number" id="cfg-movie-min" min="1" step="1" value="${Math.round((c.movie_threshold_seconds||0)/60)}" style="width:6rem;"></label>
            <label><span class="muted">Every N items</span>
                <input type="number" id="cfg-every-n" min="1" step="1" value="${escapeHtml(String(g.every_n_items))}" style="width:6rem;"></label>
            <label><span class="muted">Every M minutes</span>
                <input type="number" id="cfg-every-m" min="0" step="0.5" value="${escapeHtml(String(g.every_m_minutes))}" style="width:6rem;"></label>
            <label><span class="muted">No-repeat</span>
                <input type="checkbox" id="cfg-no-repeat" ${g.no_repeat ? 'checked' : ''}></label>
        </div>
        <table class="admin-table" style="margin-top:1rem;">
            <tr><th>Type</th><th>Enabled</th><th>Order</th><th>Weight</th></tr>
            ${typeRows}
        </table>
        <div style="margin-top:1rem;">
            <button type="submit" class="btn btn-primary">Save Settings</button>
        </div>`;
}

function collectSettings() {
    const c = JSON.parse(JSON.stringify(CURRENT_CONFIG || {}));
    c.enabled = document.getElementById('cfg-enabled').checked;
    c.movie_threshold_seconds = Math.max(1, parseInt(document.getElementById('cfg-movie-min').value, 10) || 0) * 60;
    c.general = c.general || {};
    c.general.every_n_items = Math.max(1, parseInt(document.getElementById('cfg-every-n').value, 10) || 1);
    c.general.every_m_minutes = Math.max(0, parseFloat(document.getElementById('cfg-every-m').value) || 0);
    c.general.no_repeat = document.getElementById('cfg-no-repeat').checked;
    c.types = c.types || {};
    document.querySelectorAll('#settings-view tr[data-type]').forEach(row => {
        const t = row.getAttribute('data-type');
        const tc = c.types[t] || {};
        const enabledEl = row.querySelector('[data-field="enabled"]');
        const orderEl = row.querySelector('[data-field="order"]');
        const weightEl = row.querySelector('[data-field="weight"]');
        if (enabledEl) tc.enabled = enabledEl.checked;
        if (orderEl) tc.order = orderEl.value;
        if (weightEl) tc.weight = Math.max(0, parseInt(weightEl.value, 10) || 0);
        c.types[t] = tc;
    });
    return c;
}

document.getElementById('settings-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const resp = await fetch('/admin/promos/config', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(collectSettings())
    });
    if (resp.ok) {
        showToast('Promo settings saved', 'success');
        loadSettings();
    } else {
        const data = await resp.json().catch(() => ({}));
        showToast(data.detail || 'Save failed', 'error');
    }
});

loadPools();
loadSettings();
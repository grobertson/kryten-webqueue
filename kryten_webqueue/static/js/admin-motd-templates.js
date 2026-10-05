/* Admin MOTD templates, schedules, and history (docs/MOTD_TEMPLATES_SPEC.md). */

const MT = {
    templates: [],
    current: null,        // template being edited (null = new, unsaved)
    newKind: 'master',
    automation: null,
    previewTimer: null,
    previewSeq: 0,
};

async function mtJson(url, options = {}) {
    const resp = await fetch(url, {
        headers: options.body && !(options.body instanceof FormData)
            ? { 'Content-Type': 'application/json' } : {},
        ...options,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        const detail = data.detail;
        const message = typeof detail === 'string' ? detail
            : (detail && detail.message) ? detail.message + (detail.templates ? `: ${detail.templates.join(', ')}` : '')
            : `Request failed (${resp.status})`;
        const error = new Error(message);
        error.line = detail && detail.line;
        throw error;
    }
    return data;
}

function mtLocal(iso) {
    return iso ? iso.slice(0, 16).replace('T', ' ') : '—';
}

function mtModal(title, bodyHtml, actions) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-box modal-wide" role="dialog" aria-modal="true">
            <h3>${escapeHtml(title)}</h3>
            <div class="modal-body">${bodyHtml}</div>
            <div class="modal-actions">
                ${actions.map((a, i) => `<button class="btn ${a.cls || 'btn-secondary'}" data-i="${i}">${escapeHtml(a.label)}</button>`).join('')}
            </div>
        </div>`;
    const close = () => overlay.remove();
    overlay.addEventListener('click', async (e) => {
        if (e.target === overlay) return close();
        const i = e.target.getAttribute('data-i');
        if (i === null) return;
        const keep = await actions[Number(i)].run?.(overlay);
        if (keep !== true) close();
    });
    document.body.appendChild(overlay);
    return overlay;
}

// ---------- tabs ----------

function mtShowTab(name) {
    document.querySelectorAll('[data-motd-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.motdTab === name);
    });
    document.querySelectorAll('[id^="motd-tab-"]').forEach(panel => {
        panel.hidden = panel.id !== `motd-tab-${name}`;
    });
    if (name === 'templates') loadTemplates();
    if (name === 'schedule') loadSchedules();
    if (name === 'history') loadHistory();
}

// ---------- templates ----------

async function loadTemplates() {
    const archived = document.getElementById('tpl-show-archived').checked;
    const [tpl, auto] = await Promise.all([
        mtJson(`/admin/motd/templates?archived=${archived}`),
        mtJson('/admin/motd/automation').catch(() => null),
    ]);
    MT.templates = tpl.templates;
    MT.automation = auto;
    const list = document.getElementById('tpl-list');
    const group = (kind, label) => {
        const rows = MT.templates.filter(t => t.kind === kind);
        return `<h3 class="motd-night">${label}</h3>` + (rows.length ? rows.map(t => `
            <button type="button" class="motd-tpl-item ${MT.current && MT.current.name === t.name ? 'active' : ''}" data-tpl="${escapeHtml(t.name)}">
                <span class="motd-slot-title">${escapeHtml(t.display_name)}</span>
                <span class="muted">${escapeHtml(t.name)} · r${t.revision_no}</span>
                <span>
                    ${t.live ? '<span class="badge badge-accent">live</span>' : ''}
                    ${t.is_default ? '<span class="badge">default</span>' : ''}
                    ${t.archived_at ? '<span class="badge badge-warn">archived</span>' : ''}
                    ${t.zones.length ? `<span class="muted">zones: ${t.zones.map(escapeHtml).join(', ')}</span>` : ''}
                </span>
            </button>`).join('') : '<p class="muted">None yet.</p>');
    };
    list.innerHTML = group('master', 'Masters') + group('fragment', 'Fragments');
    list.querySelectorAll('[data-tpl]').forEach(btn =>
        btn.addEventListener('click', () => openTemplate(btn.dataset.tpl)));
    fillHostSelect();
}

function fillHostSelect() {
    const host = document.getElementById('pv-host');
    const masters = MT.templates.filter(t => t.kind === 'master' && !t.archived_at);
    host.innerHTML = '<option value="">Host: scheduled master</option>' + masters.map(m =>
        `<option value="${escapeHtml(m.name)}">Host: ${escapeHtml(m.display_name)}</option>`).join('');
}

async function openTemplate(name) {
    const t = await mtJson(`/admin/motd/templates/${encodeURIComponent(name)}`);
    MT.current = t;
    showEditor();
    document.getElementById('tpl-body').value = t.body;
    loadTemplates();
    schedulePreview(0);
}

function newTemplate() {
    mtModal('New template', `
        <div class="field"><label>Kind</label>
            <select id="nt-kind">
                <option value="master">Master (whole MOTD; must contain the movie grid)</option>
                <option value="fragment">Fragment (fills a zone of a master)</option>
            </select></div>`, [
        { label: 'Cancel' },
        {
            label: 'Start editing', cls: 'btn-primary', run: (o) => {
                MT.current = null;
                MT.newKind = o.querySelector('#nt-kind').value;
                showEditor();
                document.getElementById('tpl-body').value = MT.newKind === 'master'
                    ? '<style>\n  .my-theme { text-align: center; }\n</style>\n<div class="my-theme">\n  <h2>{{ headline }}</h2>\n  {{ movie_grid() }}\n  {{ zone("promo") }}\n</div>\n'
                    : '<div style="text-align:center">\n  <strong>Something special tonight!</strong>\n</div>\n';
                schedulePreview(0);
            },
        },
    ]);
}

function showEditor() {
    const t = MT.current;
    const kind = t ? t.kind : MT.newKind;
    document.getElementById('tpl-editor').hidden = false;
    document.getElementById('tpl-title').textContent = t ? `${t.display_name} (${t.kind})` : `New ${kind}`;
    document.getElementById('tpl-meta').textContent = t
        ? `${t.name} · revision ${t.revision_no} by ${t.revision_by || t.created_by} at ${mtLocal(t.revision_at)}`
          + (t.note ? ` — “${t.note}”` : '')
        : 'Unsaved. Use “Save as…” to name it.';
    document.getElementById('tpl-save').hidden = !t || !!t.archived_at;
    document.getElementById('tpl-revisions').hidden = !t;
    document.getElementById('tpl-archive').hidden = !t || !!t.archived_at || t.is_default;
    document.getElementById('tpl-default').hidden = !t || kind !== 'master' || t.is_default || !!t.archived_at;
    document.getElementById('pv-host').hidden = kind !== 'fragment';
    const banner = document.getElementById('tpl-live-banner');
    const auto = MT.automation && MT.automation.enabled;
    banner.hidden = !(t && t.live);
    banner.textContent = auto
        ? 'This template is live. Saving changes publishes them to the channel within about a minute.'
        : 'This template is live. Saved changes reach the channel on the next publish (automation is off).';
}

function schedulePreview(delay = 600) {
    clearTimeout(MT.previewTimer);
    MT.previewTimer = setTimeout(runPreview, delay);
}

async function runPreview() {
    const seq = ++MT.previewSeq;
    const kind = MT.current ? MT.current.kind : MT.newKind;
    const status = document.getElementById('pv-status');
    status.textContent = 'Rendering…';
    const asOf = document.getElementById('pv-asof').value;
    const payload = {
        body: document.getElementById('tpl-body').value,
        kind,
        week: document.getElementById('pv-week').value,
        grid: document.getElementById('pv-grid').value,
        as_of: asOf || null,
        host_master: kind === 'fragment' ? (document.getElementById('pv-host').value || null) : null,
    };
    let data;
    try {
        data = await mtJson('/admin/motd/templates/preview', { method: 'POST', body: JSON.stringify(payload) });
    } catch (e) {
        if (seq === MT.previewSeq) status.innerHTML = `<span class="badge badge-warn">${escapeHtml(e.message)}</span>`;
        return;
    }
    if (seq !== MT.previewSeq) return;
    if (!data.ok) {
        status.innerHTML = `<span class="badge badge-warn">Error${data.line ? ` (line ${data.line})` : ''}</span> ${escapeHtml(data.error)}`;
        return;
    }
    const pct = Math.round((data.chars / data.max_chars) * 100);
    status.innerHTML = `${data.chars.toLocaleString()} / ${data.max_chars.toLocaleString()} characters (${pct}%)`
        + (data.notes || []).map(n => ` · ${escapeHtml(n)}`).join('')
        + (data.warnings || []).map(w => ` <span class="badge badge-warn">${escapeHtml(w)}</span>`).join('');
    document.getElementById('pv-frame').srcdoc =
        '<!doctype html><meta charset="utf-8"><body style="background:#111;color:#ddd;font-family:sans-serif">'
        + data.html + '</body>';
}

async function saveTemplate() {
    const t = MT.current;
    if (!t) return saveTemplateAs();
    mtModal(`Save ${t.display_name}`, `
        <div class="field"><label for="sv-note">What changed?</label>
        <input type="text" id="sv-note" maxlength="500" placeholder="e.g. Halloween art swap"></div>`, [
        { label: 'Cancel' },
        {
            label: 'Save', cls: 'btn-primary', run: async (o) => {
                const note = o.querySelector('#sv-note').value.trim();
                if (!note) { showToast('A short note is required', 'error'); return true; }
                try {
                    const saved = await mtJson(`/admin/motd/templates/${encodeURIComponent(t.name)}`, {
                        method: 'PUT',
                        body: JSON.stringify({
                            body: document.getElementById('tpl-body').value,
                            note,
                            expected_revision_id: t.current_revision_id,
                        }),
                    });
                    MT.current = saved;
                    showEditor();
                    showToast(saved.live ? 'Saved — this template is live' : 'Saved', 'success');
                    (saved.warnings || []).forEach(w => showToast(w, 'error'));
                    loadTemplates();
                } catch (e) {
                    showToast(e.line ? `Line ${e.line}: ${e.message}` : e.message, 'error');
                    return true;
                }
            },
        },
    ]);
}

function saveTemplateAs() {
    const kind = MT.current ? MT.current.kind : MT.newKind;
    mtModal(`Save as new ${kind}`, `
        <div class="field"><label for="sa-name">Name (URL-safe)</label>
            <input type="text" id="sa-name" placeholder="halloween-2026" pattern="[a-z0-9][a-z0-9_-]{1,63}"></div>
        <div class="field"><label for="sa-display">Display name</label>
            <input type="text" id="sa-display" placeholder="Halloween 2026"></div>
        <div class="field"><label for="sa-desc">Description</label>
            <input type="text" id="sa-desc"></div>`, [
        { label: 'Cancel' },
        {
            label: 'Create', cls: 'btn-primary', run: async (o) => {
                const name = o.querySelector('#sa-name').value.trim();
                try {
                    const created = await mtJson('/admin/motd/templates', {
                        method: 'POST',
                        body: JSON.stringify({
                            name,
                            kind,
                            display_name: o.querySelector('#sa-display').value.trim() || name,
                            description: o.querySelector('#sa-desc').value.trim() || null,
                            body: document.getElementById('tpl-body').value,
                            note: MT.current ? `Copied from ${MT.current.name} r${MT.current.revision_no}` : 'Created',
                        }),
                    });
                    MT.current = created;
                    showEditor();
                    showToast(`Created ${created.name}`, 'success');
                    loadTemplates();
                } catch (e) {
                    showToast(e.line ? `Line ${e.line}: ${e.message}` : e.message, 'error');
                    return true;
                }
            },
        },
    ]);
}

async function showRevisions() {
    const t = MT.current;
    const { revisions } = await mtJson(`/admin/motd/templates/${encodeURIComponent(t.name)}/revisions`);
    const overlay = mtModal(`Revisions of ${t.display_name}`, `
        <table class="data-table"><thead><tr><th>#</th><th>When</th><th>Who</th><th>Note</th><th></th></tr></thead><tbody>
        ${revisions.map(r => `<tr>
            <td>r${r.revision_no}${r.current ? ' <span class="badge">current</span>' : ''}</td>
            <td>${mtLocal(r.created_at)}</td><td>${escapeHtml(r.created_by)}</td><td>${escapeHtml(r.note || '')}</td>
            <td><button class="btn btn-sm" data-load="${r.revision_no}">Load into editor</button>
                ${r.current ? '' : `<button class="btn btn-sm" data-restore="${r.revision_no}">Restore</button>`}</td>
        </tr>`).join('')}</tbody></table>
        <p class="muted">“Load” copies an old version into the editor without saving. “Restore” saves it as a new revision.</p>`,
        [{ label: 'Close' }]);
    overlay.querySelectorAll('[data-load]').forEach(btn => btn.addEventListener('click', async () => {
        const rev = await mtJson(`/admin/motd/templates/${encodeURIComponent(t.name)}/revisions/${btn.dataset.load}`);
        document.getElementById('tpl-body').value = rev.body;
        overlay.remove();
        schedulePreview(0);
        showToast(`Loaded r${rev.revision_no} — not saved yet`, 'success');
    }));
    overlay.querySelectorAll('[data-restore]').forEach(btn => btn.addEventListener('click', async () => {
        try {
            const saved = await mtJson(
                `/admin/motd/templates/${encodeURIComponent(t.name)}/revisions/${btn.dataset.restore}/restore`,
                { method: 'POST' });
            overlay.remove();
            MT.current = saved;
            document.getElementById('tpl-body').value = saved.body;
            showEditor();
            loadTemplates();
            schedulePreview(0);
            showToast(`Restored as r${saved.revision_no}`, 'success');
        } catch (e) { showToast(e.message, 'error'); }
    }));
}

async function makeDefault() {
    const t = MT.current;
    if (!confirm(`Make “${t.display_name}” the default master? It shows whenever no master schedule is active.`)) return;
    try {
        MT.current = { ...MT.current, ...(await mtJson(`/admin/motd/templates/${encodeURIComponent(t.name)}`, {
            method: 'PATCH', body: JSON.stringify({ is_default: true }),
        })) };
        showEditor();
        loadTemplates();
        showToast('Default master updated', 'success');
    } catch (e) { showToast(e.message, 'error'); }
}

async function archiveTemplate() {
    const t = MT.current;
    if (!confirm(`Archive “${t.display_name}”? It disappears from lists and schedules; history is kept.`)) return;
    try {
        await mtJson(`/admin/motd/templates/${encodeURIComponent(t.name)}/archive`, { method: 'POST' });
        MT.current = null;
        document.getElementById('tpl-editor').hidden = true;
        loadTemplates();
        showToast('Archived', 'success');
    } catch (e) { showToast(e.message, 'error'); }
}

async function insertMedia() {
    const { assets } = await mtJson('/admin/media/assets?limit=200');
    const overlay = mtModal('Insert media', `
        <p class="muted">Pick an image to insert at the cursor. <a href="/admin/media" target="_blank" rel="noopener">Upload new art</a>, then reopen this list.</p>
        <div class="media-grid">${assets.map(a => `
            <button type="button" class="media-card" data-snippet="${escapeHtml(a.snippet)}">
                <img src="${escapeHtml(a.url)}" alt="" loading="lazy">
                <span class="muted">${escapeHtml(a.slug)}</span>
            </button>`).join('') || '<p class="muted">No uploads yet.</p>'}</div>`, [{ label: 'Close' }]);
    overlay.querySelectorAll('[data-snippet]').forEach(btn => btn.addEventListener('click', () => {
        const area = document.getElementById('tpl-body');
        const start = area.selectionStart;
        const snippet = btn.dataset.snippet;
        area.value = area.value.slice(0, start) + snippet + area.value.slice(area.selectionEnd);
        area.selectionStart = area.selectionEnd = start + snippet.length;
        overlay.remove();
        area.focus();
        schedulePreview(0);
    }));
}

// ---------- schedules ----------

const WEEKDAYS = [['MO', 'Mon'], ['TU', 'Tue'], ['WE', 'Wed'], ['TH', 'Thu'], ['FR', 'Fri'], ['SA', 'Sat'], ['SU', 'Sun']];

async function loadSchedules() {
    const [{ schedules, timezone }, timeline, tpl] = await Promise.all([
        mtJson('/admin/motd/schedules'),
        mtJson('/admin/motd/timeline'),
        mtJson('/admin/motd/templates'),
    ]);
    MT.templates = tpl.templates;
    document.getElementById('sch-tz').textContent = timezone;
    const describe = s => s.rrule
        ? `${escapeHtml(s.rrule)} · ${s.duration_minutes} min from ${mtLocal(s.starts_at_local)}${s.ends_at ? ` until ${mtLocal(s.ends_at_local)}` : ''}`
        : `${mtLocal(s.starts_at_local)} → ${mtLocal(s.ends_at_local)}`;
    document.getElementById('sch-list').innerHTML = schedules.length ? `
        <table class="data-table"><thead><tr><th>Label</th><th>Template</th><th>Zone</th><th>When (Eastern)</th><th>Priority</th><th></th></tr></thead><tbody>
        ${schedules.map(s => `<tr class="${s.is_active ? '' : 'muted'}">
            <td>${escapeHtml(s.label)}${s.is_active ? '' : ' <span class="badge">paused</span>'}</td>
            <td>${escapeHtml(s.template_display_name || s.template)} <span class="muted">(${s.template_kind})</span></td>
            <td>${escapeHtml(s.zone || '—')}</td><td>${describe(s)}</td><td>${s.priority}</td>
            <td><button class="btn btn-sm" data-edit-sch="${s.id}">Edit</button>
                <button class="btn btn-sm btn-danger" data-del-sch="${s.id}">Delete</button></td>
        </tr>`).join('')}</tbody></table>` : '<p class="muted">No schedules: the default master is always shown.</p>';
    document.querySelectorAll('[data-edit-sch]').forEach(b => b.addEventListener('click', () =>
        editSchedule(schedules.find(s => String(s.id) === b.dataset.editSch))));
    document.querySelectorAll('[data-del-sch]').forEach(b => b.addEventListener('click', async () => {
        if (!confirm('Delete this schedule?')) return;
        try {
            await mtJson(`/admin/motd/schedules/${b.dataset.delSch}`, { method: 'DELETE' });
            loadSchedules();
        } catch (e) { showToast(e.message, 'error'); }
    }));

    document.getElementById('sch-warnings').innerHTML = (timeline.warnings || [])
        .map(w => `<div class="badge badge-warn">${escapeHtml(w)}</div>`).join(' ');
    document.getElementById('sch-timeline').innerHTML = `
        <table class="data-table"><thead><tr><th>From</th><th>Until</th><th>Master</th><th>Zones</th></tr></thead><tbody>
        ${timeline.segments.map(seg => `<tr>
            <td>${mtLocal(seg.start_local)}</td><td>${mtLocal(seg.end_local)}</td>
            <td>${escapeHtml(seg.master || '—')}</td>
            <td>${Object.entries(seg.zones).map(([z, f]) =>
                `${escapeHtml(z)}: ${escapeHtml(f.template || '?')}${f.hidden ? ' <span class="badge badge-warn">hidden</span>' : ''}`).join('<br>') || '<span class="muted">—</span>'}</td>
        </tr>`).join('')}</tbody></table>`;
}

function editSchedule(existing) {
    const s = existing || {};
    const weekly = s.rrule && /^FREQ=WEEKLY;BYDAY=([A-Z,]+)$/.exec(s.rrule);
    const mode = !s.id ? 'once' : (!s.rrule ? 'once' : (weekly ? 'weekly' : 'advanced'));
    const start = (s.starts_at_local || '').slice(0, 16);
    const end = (s.ends_at_local || '').slice(0, 16);
    const startTime = start.slice(11, 16) || '18:00';
    const endTime = (() => {
        if (!s.duration_minutes || !start) return '23:00';
        const [h, m] = startTime.split(':').map(Number);
        const total = (h * 60 + m + s.duration_minutes) % 1440;
        return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
    })();
    const days = weekly ? weekly[1].split(',') : [];
    const templates = MT.templates.filter(t => !t.archived_at);
    const zones = [...new Set(MT.templates.flatMap(t => t.zones))];
    const overlay = mtModal(s.id ? 'Edit schedule' : 'New schedule', `
        <div class="field"><label for="se-template">Template</label>
            <select id="se-template">${templates.map(t =>
                `<option value="${escapeHtml(t.name)}" data-kind="${t.kind}" ${t.name === s.template ? 'selected' : ''}>${escapeHtml(t.display_name)} (${t.kind})</option>`).join('')}</select></div>
        <div class="field" id="se-zone-field"><label for="se-zone">Zone</label>
            <input type="text" id="se-zone" list="se-zones" value="${escapeHtml(s.zone || 'promo')}">
            <datalist id="se-zones">${zones.map(z => `<option value="${escapeHtml(z)}">`).join('')}</datalist></div>
        <div class="field"><label for="se-label">Label</label>
            <input type="text" id="se-label" value="${escapeHtml(s.label || '')}" placeholder="Halloween week"></div>
        <div class="field"><label>Shape</label>
            <select id="se-mode">
                <option value="once" ${mode === 'once' ? 'selected' : ''}>One window</option>
                <option value="weekly" ${mode === 'weekly' ? 'selected' : ''}>Weekly hours</option>
                <option value="advanced" ${mode === 'advanced' ? 'selected' : ''}>Advanced (RRULE)</option>
            </select></div>
        <div data-mode="once">
            <div class="field"><label>Start</label><input type="datetime-local" id="se-start" value="${start}"></div>
            <div class="field"><label>End</label><input type="datetime-local" id="se-end" value="${end}"></div>
        </div>
        <div data-mode="weekly">
            <div class="field"><label>Days</label><div>${WEEKDAYS.map(([code, label]) =>
                `<label><input type="checkbox" data-day="${code}" ${days.includes(code) ? 'checked' : ''}> ${label}</label>`).join(' ')}</div></div>
            <div class="field"><label>From / to (each day)</label>
                <div><input type="time" id="se-time-start" value="${startTime}"> – <input type="time" id="se-time-end" value="${endTime}"></div></div>
            <div class="field"><label>Series starts</label><input type="date" id="se-series-start" value="${start.slice(0, 10)}"></div>
            <div class="field"><label>Series ends (optional)</label><input type="date" id="se-series-end" value="${end.slice(0, 10)}"></div>
        </div>
        <div data-mode="advanced">
            <div class="field"><label>RRULE (no DTSTART/UNTIL/COUNT)</label><input type="text" id="se-rrule" value="${escapeHtml(s.rrule || 'FREQ=WEEKLY;BYDAY=TH')}"></div>
            <div class="field"><label>Occurrence length (minutes)</label><input type="number" id="se-duration" min="1" max="10080" value="${s.duration_minutes || 300}"></div>
            <div class="field"><label>Series start</label><input type="datetime-local" id="se-adv-start" value="${start}"></div>
            <div class="field"><label>Series end (optional)</label><input type="datetime-local" id="se-adv-end" value="${end}"></div>
        </div>
        <div class="field"><label for="se-priority">Priority (higher wins)</label>
            <input type="number" id="se-priority" value="${s.priority || 0}" min="-1000" max="1000"></div>
        <label><input type="checkbox" id="se-active" ${s.is_active === false ? '' : 'checked'}> Active</label>`, [
        { label: 'Cancel' },
        { label: 'Save', cls: 'btn-primary', run: (o) => submitSchedule(o, s.id) },
    ]);
    const sync = () => {
        const m = overlay.querySelector('#se-mode').value;
        overlay.querySelectorAll('[data-mode]').forEach(d => { d.hidden = d.dataset.mode !== m; });
        const opt = overlay.querySelector('#se-template').selectedOptions[0];
        overlay.querySelector('#se-zone-field').hidden = !opt || opt.dataset.kind !== 'fragment';
    };
    overlay.querySelector('#se-mode').addEventListener('change', sync);
    overlay.querySelector('#se-template').addEventListener('change', sync);
    sync();
}

async function submitSchedule(o, id) {
    const q = sel => o.querySelector(sel);
    const opt = q('#se-template').selectedOptions[0];
    const payload = {
        template: q('#se-template').value,
        zone: opt && opt.dataset.kind === 'fragment' ? q('#se-zone').value.trim() : null,
        label: q('#se-label').value.trim(),
        priority: Number(q('#se-priority').value || 0),
        is_active: q('#se-active').checked,
        rrule: null, duration_minutes: null, ends_at: null,
    };
    const mode = q('#se-mode').value;
    if (mode === 'once') {
        payload.starts_at = q('#se-start').value;
        payload.ends_at = q('#se-end').value || null;
    } else if (mode === 'weekly') {
        const days = [...o.querySelectorAll('[data-day]:checked')].map(c => c.dataset.day);
        if (!days.length) { showToast('Pick at least one day', 'error'); return true; }
        const [sh, sm] = q('#se-time-start').value.split(':').map(Number);
        const [eh, em] = q('#se-time-end').value.split(':').map(Number);
        let minutes = (eh * 60 + em) - (sh * 60 + sm);
        if (minutes <= 0) minutes += 1440;  // overnight window
        payload.rrule = `FREQ=WEEKLY;BYDAY=${days.join(',')}`;
        payload.duration_minutes = minutes;
        payload.starts_at = `${q('#se-series-start').value}T${q('#se-time-start').value}`;
        payload.ends_at = q('#se-series-end').value ? `${q('#se-series-end').value}T23:59` : null;
    } else {
        payload.rrule = q('#se-rrule').value.trim();
        payload.duration_minutes = Number(q('#se-duration').value);
        payload.starts_at = q('#se-adv-start').value;
        payload.ends_at = q('#se-adv-end').value || null;
    }
    try {
        await mtJson(id ? `/admin/motd/schedules/${id}` : '/admin/motd/schedules', {
            method: id ? 'PUT' : 'POST', body: JSON.stringify(payload),
        });
        showToast('Schedule saved', 'success');
        loadSchedules();
    } catch (e) {
        showToast(e.message, 'error');
        return true;
    }
}

// ---------- history ----------

async function loadHistory() {
    const [auto, pubs, audit] = await Promise.all([
        mtJson('/admin/motd/automation'),
        mtJson('/admin/motd/publications'),
        mtJson('/admin/audit?limit=100'),
    ]);
    MT.automation = auto;
    document.getElementById('auto-enabled').checked = auto.enabled;
    const desired = auto.desired;
    const zones = desired ? Object.entries(desired.zones).map(([z, f]) => `${z}: ${f ? f.template : 'empty'}`).join(', ') : '';
    document.getElementById('auto-status').innerHTML = auto.error
        ? `<span class="badge badge-warn">${escapeHtml(auto.error)}</span>`
        : `Should be live now: <strong>${escapeHtml(desired.master)}</strong>${zones ? ` (${escapeHtml(zones)})` : ''}.
           ${auto.in_sync ? '<span class="badge">in sync</span>' : '<span class="badge badge-warn">not yet published</span>'}
           Last publish: ${mtLocal(auto.latest_publication_at)} UTC. Checked every ${auto.interval_seconds}s when enabled.`;

    document.getElementById('pub-list').innerHTML = pubs.publications.length ? `
        <table class="data-table"><thead><tr><th>When (UTC)</th><th>Trigger</th><th>By</th><th>Weekend</th><th>Master</th><th>Zones</th><th>Size</th><th>Run</th></tr></thead><tbody>
        ${pubs.publications.map(p => `<tr>
            <td>${mtLocal(p.published_at)}</td><td>${escapeHtml(p.trigger)}</td><td>${escapeHtml(p.published_by)}</td>
            <td>${escapeHtml(p.week_key)}</td><td>${escapeHtml(p.master || '?')}</td>
            <td>${Object.entries(p.zones).map(([z, n]) => `${escapeHtml(z)}: ${escapeHtml(n || 'empty')}`).join('<br>')}</td>
            <td>${p.html_chars}</td><td>${p.job_run_id ?? ''}</td>
        </tr>`).join('')}</tbody></table>` : '<p class="muted">Nothing published from templates yet.</p>';

    document.getElementById('audit-list').innerHTML = audit.entries.length ? `
        <table class="data-table"><thead><tr><th>When (UTC)</th><th>Who</th><th>Action</th><th>Summary</th></tr></thead><tbody>
        ${audit.entries.map(e => `<tr><td>${mtLocal(e.at)}</td><td>${escapeHtml(e.actor)}</td>
            <td>${escapeHtml(e.action)}</td><td>${escapeHtml(e.summary || '')}</td></tr>`).join('')}
        </tbody></table>` : '<p class="muted">No entries in the last 30 days.</p>';
}

async function toggleAutomation(e) {
    const enabled = e.target.checked;
    if (enabled && !confirm('Turn on automatic publishing? The channel MOTD will follow the schedules from now on, replacing any hand edits.')) {
        e.target.checked = false;
        return;
    }
    try {
        await mtJson('/admin/motd/automation', { method: 'PUT', body: JSON.stringify({ enabled }) });
        showToast(`Automation ${enabled ? 'on' : 'off'}`, 'success');
        loadHistory();
    } catch (err) {
        e.target.checked = !enabled;
        showToast(err.message, 'error');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-motd-tab]').forEach(btn =>
        btn.addEventListener('click', () => mtShowTab(btn.dataset.motdTab)));
    document.getElementById('tpl-new').addEventListener('click', newTemplate);
    document.getElementById('tpl-show-archived').addEventListener('change', loadTemplates);
    document.getElementById('tpl-save').addEventListener('click', saveTemplate);
    document.getElementById('tpl-save-as').addEventListener('click', saveTemplateAs);
    document.getElementById('tpl-revisions').addEventListener('click', showRevisions);
    document.getElementById('tpl-default').addEventListener('click', makeDefault);
    document.getElementById('tpl-archive').addEventListener('click', archiveTemplate);
    document.getElementById('tpl-insert-media').addEventListener('click', insertMedia);
    document.getElementById('tpl-body').addEventListener('input', () => schedulePreview());
    ['pv-week', 'pv-grid', 'pv-asof', 'pv-host'].forEach(id =>
        document.getElementById(id).addEventListener('change', () => schedulePreview(0)));
    document.getElementById('pv-refresh').addEventListener('click', () => schedulePreview(0));
    document.getElementById('tpl-body').addEventListener('keydown', (e) => {
        if (e.key === 'Tab') {
            e.preventDefault();
            const a = e.target;
            const s = a.selectionStart;
            a.value = a.value.slice(0, s) + '  ' + a.value.slice(a.selectionEnd);
            a.selectionStart = a.selectionEnd = s + 2;
        }
    });
    document.getElementById('sch-new').addEventListener('click', () => editSchedule(null));
    document.getElementById('auto-enabled').addEventListener('change', toggleAutomation);
});

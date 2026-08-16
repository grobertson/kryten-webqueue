async function clearQueue() {
    if (!confirm('Clear the entire queue? All pay items will be refunded.')) return;
    const resp = await fetch('/admin/queue/clear', {method: 'POST'});
    const data = await resp.json();
    showToast(resp.ok ? `Cleared! ${data.refunded} items refunded.` : 'Failed', resp.ok ? 'success' : 'error');
}

async function triggerSync() {
    await runJob('catalog_sync');
}

// Cache of the most recent /admin/jobs payload so the Run buttons know each
// job's parameter schema without an extra round-trip.
let JOBS_CACHE = [];
// Map of job_name → schedule row from /admin/job-schedules.
let SCHEDULES_CACHE = {};

async function runJob(name, params) {
    const resp = await fetch(`/admin/jobs/${name}/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params ? { params } : {})
    });
    if (resp.ok) {
        showToast('Job started', 'success');
    } else if (resp.status === 409) {
        showToast('Job already running', 'error');
    } else if (resp.status === 400) {
        const data = await resp.json().catch(() => ({}));
        showToast(data.detail || 'Invalid parameters', 'error');
    } else {
        showToast('Failed to start job', 'error');
    }
    loadJobs();
}

// Entry point for a Run button: jobs with a schema open a parameter modal,
// jobs without one run immediately.
function startJob(name) {
    const job = JOBS_CACHE.find(j => j.name === name);
    if (job && Array.isArray(job.schema) && job.schema.length) {
        openJobRunnerModal(job);
    } else {
        runJob(name);
    }
}

function _jobFieldHtml(f, prefix = 'jobparam-') {
    const id = `${prefix}${f.name}`;
    const label = escapeHtml(f.label || f.name);
    const def = f.default;
    const helpHtml = f.help ? `<span class="field-help">${escapeHtml(f.help)}</span>` : '';
    if (f.type === 'bool') {
        return `<label class="field field-inline">
            <input type="checkbox" id="${id}" ${def ? 'checked' : ''}>
            <span>${label}</span>
        </label>${helpHtml ? `<div style="margin:-0.5rem 0 0.85rem 0">${helpHtml}</div>` : ''}`;
    }
    if (f.type === 'enum') {
        const opts = (f.options || []).map(o => {
            const val = (o && typeof o === 'object') ? o.value : o;
            const txt = (o && typeof o === 'object') ? (o.label || o.value) : o;
            const sel = (val === def) ? 'selected' : '';
            return `<option value="${escapeHtml(String(val))}" ${sel}>${escapeHtml(String(txt))}</option>`;
        }).join('');
        return `<label class="field"><span>${label}</span>
            <select id="${id}">${opts}</select>${helpHtml}</label>`;
    }
    if (f.type === 'playlist') {
        return `<label class="field"><span>${label}</span>
            <select id="${id}" data-playlist-picker="1"><option value="">—</option></select>${helpHtml}</label>`;
    }
    const inputType = (f.type === 'int' || f.type === 'float') ? 'number' : 'text';
    const step = f.type === 'float' ? ' step="any"' : '';
    const val = (def === null || def === undefined) ? '' : escapeHtml(String(def));
    const placeholder = f.placeholder ? ` placeholder="${escapeHtml(f.placeholder)}"` : '';
    return `<label class="field"><span>${label}${f.required ? ' *' : ''}</span>
        <input type="${inputType}"${step}${placeholder} id="${id}" value="${val}">${helpHtml}</label>`;
}

async function openJobRunnerModal(job) {
    closeJobRunnerModal();
    const overlay = document.createElement('div');
    overlay.id = 'job-runner-modal';
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-box" role="dialog" aria-modal="true">
            <h3>Run: ${escapeHtml(job.label)}</h3>
            <div class="modal-body"><div class="job-params">${job.schema.map(f => _jobFieldHtml(f)).join('')}</div></div>
            <div class="modal-actions">
                <button class="btn btn-secondary" data-action="cancel">Cancel</button>
                <button class="btn btn-primary" data-action="run">Run</button>
            </div>
        </div>`;
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closeJobRunnerModal();
        const action = e.target.getAttribute('data-action');
        if (action === 'cancel') closeJobRunnerModal();
        if (action === 'run') submitJobRunner(job);
    });
    document.body.appendChild(overlay);

    // Populate any playlist pickers.
    const pickers = overlay.querySelectorAll('[data-playlist-picker]');
    if (pickers.length) {
        try {
            const r = await fetch('/admin/playlists/', { credentials: 'same-origin' });
            if (r.ok) {
                const playlists = await r.json();
                const opts = playlists.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');
                pickers.forEach(sel => { sel.innerHTML = '<option value="">—</option>' + opts; });
            }
        } catch (e) { /* leave empty */ }
    }
}

function submitJobRunner(job) {
    const params = {};
    for (const f of job.schema) {
        const el = document.getElementById(`jobparam-${f.name}`);
        if (!el) continue;
        if (f.type === 'bool') {
            params[f.name] = el.checked;
        } else if (el.value !== '') {
            params[f.name] = el.value;
        }
    }
    closeJobRunnerModal();
    runJob(job.name, params);
}

function closeJobRunnerModal() {
    const existing = document.getElementById('job-runner-modal');
    if (existing) existing.remove();
}

// ── Job Schedule UI ────────────────────────────────────────────────────────────

async function openScheduleModal(jobName) {
    const job = JOBS_CACHE.find(j => j.name === jobName);
    if (!job) return;
    const existing = SCHEDULES_CACHE[jobName] || null;
    let existingParams = {};
    if (existing && existing.params_json) {
        try { existingParams = JSON.parse(existing.params_json); } catch (e) {}
    }

    const cronVal = existing ? escapeHtml(existing.cron_expression || '') : '';
    const isActive = existing ? !!existing.is_active : true;

    const paramFields = job.schema.map(f => {
        const savedVal = existingParams[f.name] !== undefined ? existingParams[f.name] : f.default;
        return _jobFieldHtml({...f, default: savedVal}, 'schedparam-');
    }).join('');

    const overlay = document.createElement('div');
    overlay.id = 'schedule-modal';
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-box" role="dialog" aria-modal="true">
            <h3>${existing ? 'Edit' : 'Create'} Schedule: ${escapeHtml(job.label)}</h3>
            <div class="modal-body">
            <label class="field">
                <span>Cron expression <small style="font-weight:normal;opacity:.7">(min hour dom mon dow, UTC)</small></span>
                <input type="text" id="sched-cron" placeholder="0 21 * * 4" value="${cronVal}" autocomplete="off">
            </label>
            <label class="field field-inline">
                <input type="checkbox" id="sched-active" ${isActive ? 'checked' : ''}>
                <span>Active</span>
            </label>
            ${paramFields ? `<details ${existing ? 'open' : ''}><summary style="cursor:pointer;margin:.5rem 0">Job parameters</summary><div class="job-params">${paramFields}</div></details>` : ''}
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" data-action="cancel">Cancel</button>
                <button class="btn btn-primary" data-action="save">Save</button>
            </div>
        </div>`;

    overlay.addEventListener('click', e => {
        if (e.target === overlay) overlay.remove();
        const action = e.target.getAttribute('data-action');
        if (action === 'cancel') overlay.remove();
        if (action === 'save') _submitScheduleModal(jobName, job, overlay);
    });
    document.body.appendChild(overlay);
}

async function _submitScheduleModal(jobName, job, overlay) {
    const cron = (document.getElementById('sched-cron').value || '').trim();
    if (!cron) { showToast('Cron expression is required', 'error'); return; }
    const isActive = document.getElementById('sched-active').checked;

    const params = {};
    for (const f of job.schema) {
        const el = document.getElementById(`schedparam-${f.name}`);
        if (!el) continue;
        if (f.type === 'bool') {
            params[f.name] = el.checked;
        } else if (el.value !== '') {
            params[f.name] = el.value;
        }
    }

    const resp = await fetch('/admin/job-schedules', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            job_name: jobName,
            cron_expression: cron,
            is_active: isActive,
            params: Object.keys(params).length ? params : null,
            label: job.label,
        }),
    });
    overlay.remove();
    if (resp.ok) {
        showToast('Schedule saved', 'success');
    } else {
        const d = await resp.json().catch(() => ({}));
        showToast(d.detail || 'Failed to save schedule', 'error');
    }
    loadJobs();
}

async function removeJobSchedule(jobName) {
    const job = JOBS_CACHE.find(j => j.name === jobName);
    const label = job ? job.label : jobName;
    if (!confirm(`Remove schedule for "${label}"?`)) return;
    const resp = await fetch(`/admin/job-schedules/${encodeURIComponent(jobName)}`, {method: 'DELETE'});
    showToast(resp.ok ? 'Schedule removed' : 'Failed to remove schedule', resp.ok ? 'success' : 'error');
    loadJobs();
}

async function clearActiveSchedule() {
    const resp = await fetch('/admin/schedules/clear-active', {method: 'POST'});
    showToast(resp.ok ? 'Active schedule cleared' : 'Failed', resp.ok ? 'success' : 'error');
}

async function loadJobs() {
    // Load job schedules first so the job row renderer can reference them.
    const schResp = await fetch('/admin/job-schedules');
    if (schResp.ok) {
        SCHEDULES_CACHE = {};
        const schedules = await schResp.json();
        for (const s of schedules) SCHEDULES_CACHE[s.job_name] = s;
    }

    // Registered jobs with run buttons
    const jResp = await fetch('/admin/jobs');
    if (jResp.ok) {
        const jobs = await jResp.json();
        JOBS_CACHE = jobs;
        const el = document.getElementById('jobs-list');
        el.innerHTML = jobs.length
            ? jobs.map(j => {
                const hasParams = Array.isArray(j.schema) && j.schema.length;
                const lr = j.last_run;
                const summary = lr
                    ? `<span class="job-last-run">Last Run: <span class="job-status job-status-${escapeHtml(lr.status || '')}">${escapeHtml(lr.status || '')}</span> ${formatLocalDateTime(lr.started_at)}</span>`
                    : '<span class="job-last-run muted">never run</span>';
                const sched = SCHEDULES_CACHE[j.name];
                const schedBadge = sched
                    ? ` <span class="job-badge" title="${escapeHtml(sched.cron_expression || '')}">${sched.is_active ? 'scheduled' : 'paused'}</span>`
                    : '';
                // Column 2: edit an existing schedule. When none exists this is an
                // empty placeholder so the add/delete toggle stays anchored in column 3.
                const schedEditBtn = sched
                    ? `<button class="btn btn-sm" onclick="openScheduleModal('${j.name}')" title="Edit schedule">${sched.is_active ? '\u23f0' : '\u23f8'}</button>`
                    : '<span class="job-action-empty"></span>';
                // Column 3: toggles between "add schedule" (none) and "delete schedule".
                const schedToggleBtn = sched
                    ? `<button class="btn btn-sm btn-danger" onclick="removeJobSchedule('${j.name}')" title="Delete schedule">\u00d7</button>`
                    : `<button class="btn btn-sm" onclick="openScheduleModal('${j.name}')" title="Add schedule">\u2295</button>`;
                return `
                <div class="job-row">
                    <span class="job-label">${escapeHtml(j.label)}${hasParams ? ' <span class="job-badge">params</span>' : ''}${schedBadge}</span>
                    ${summary}
                    <div class="job-actions">
                        <button class="btn btn-sm" onclick="startJob('${j.name}')" ${j.running ? 'disabled' : ''}>${j.running ? 'Running' : (hasParams ? 'Begin' : 'Run')}</button>
                        ${schedEditBtn}
                        ${schedToggleBtn}
                    </div>
                </div>`;
            }).join('')
            : '<p>No jobs registered</p>';
    }

    // Recent job-run history — table includes an expand button for full output
    const rResp = await fetch('/admin/jobs/runs?limit=15');
    if (rResp.ok) {
        const runs = await rResp.json();
        const el = document.getElementById('job-runs');
        if (runs.length > 0) {
            el.innerHTML = `<table class="admin-table">
                <tr><th>Job</th><th>Started</th><th>Ended</th><th>Status</th><th>Detail</th><th></th></tr>
                ${runs.map(r => {
                    const summary = escapeHtml(summarizeRunDetail(r.detail));
                    const hasDetail = r.detail && r.detail.length > 2;
                    const expandBtn = hasDetail
                        ? `<button class="btn btn-sm" onclick="showRunDetail(${r.id})" title="View full output">\u2139</button>`
                        : '';
                    const endedCell = r.ended_at
                        ? formatLocalDateTime(r.ended_at)
                        : `<span style="color:var(--warning)">running\u2026</span>`;
                    return `
                    <tr>
                        <td>${escapeHtml(r.job_name || '')}</td>
                        <td>${formatLocalDateTime(r.started_at)}</td>
                        <td>${endedCell}</td>
                        <td><span class="job-status job-status-${escapeHtml(r.status || '')}">${escapeHtml(r.status || '')}</span></td>
                        <td class="job-detail">${summary}</td>
                        <td>${expandBtn}</td>
                    </tr>`;
                }).join('')}
            </table>`;
        } else {
            el.innerHTML = '<p>No job runs yet</p>';
        }
    }
}

// Run-detail cache so we don't re-fetch on re-open.
const RUN_DETAIL_CACHE = {};

async function showRunDetail(runId) {
    if (!RUN_DETAIL_CACHE[runId]) {
        const resp = await fetch('/admin/jobs/runs?limit=50');
        if (resp.ok) { (await resp.json()).forEach(r => { RUN_DETAIL_CACHE[r.id] = r; }); }
    }
    const run = RUN_DETAIL_CACHE[runId];
    if (!run) { showToast('Run not found', 'error'); return; }

    const existingModal = document.getElementById('run-detail-modal');
    if (existingModal) existingModal.remove();

    let d = null;
    try { d = run.detail ? JSON.parse(run.detail) : null; } catch (e) {}

    let bodyHtml;
    if (d && d.steps_run && d.steps) {
        // Pipeline enrichment final report — render as a table
        const dryBadge = d.dry_run
            ? ' <span style="color:var(--warning);font-size:.8rem">(dry run \u2014 no writes)</span>' : '';
        const elapsed = d.elapsed_sec ? ` \u00b7 ${d.elapsed_sec}s elapsed` : '';
        const rowsHtml = d.steps_run.map(s => {
            const r = d.steps[s];
            if (!r) return '';
            const errs = (r.errors || []).map(e =>
                `<div style="font-size:.78rem;color:var(--danger);padding:.15rem 0">${escapeHtml(e)}</div>`
            ).join('');
            return `<tr>
                <td><strong>${escapeHtml(s)}</strong></td>
                <td>${r.processed}</td>
                <td>${r.changed}</td>
                <td>${r.skipped}</td>
                <td style="${r.failed ? 'color:var(--danger)' : ''}">${r.failed || 0}</td>
            </tr>${errs ? `<tr><td colspan="5" style="padding:0 0 .5rem 1.5rem">${errs}</td></tr>` : ''}`;
        }).join('');
        bodyHtml = `
            <p style="margin-bottom:.75rem">
                Steps: <strong>${escapeHtml(d.steps_run.join(' \u2192 '))}</strong>${dryBadge}<br>
                Total items: <strong>${(d.total_items || 0).toLocaleString()}</strong>${elapsed}
            </p>
            <table class="admin-table">
                <tr><th>Step</th><th>Processed</th><th>Changed</th><th>Skipped</th><th>Failed</th></tr>
                ${rowsHtml}
            </table>`;
    } else if (d && d.step && typeof d.processed === 'number') {
        // In-progress heartbeat snapshot
        bodyHtml = `<p style="color:var(--warning)">\u23f3 Job still running — last progress snapshot:</p>
            <pre style="white-space:pre-wrap;font-size:.82rem">${escapeHtml(JSON.stringify(d, null, 2))}</pre>`;
    } else {
        // Generic / legacy format
        const raw = typeof run.detail === 'string' ? run.detail : JSON.stringify(run.detail, null, 2);
        bodyHtml = `<pre style="white-space:pre-wrap;font-size:.82rem">${escapeHtml(raw)}</pre>`;
    }

    const overlay = document.createElement('div');
    overlay.id = 'run-detail-modal';
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-box" role="dialog" aria-modal="true">
            <h3>${escapeHtml(run.job_name || 'Run')} \u2014 ${escapeHtml(run.status || '')}
                <span style="font-size:.75rem;font-weight:400;opacity:.7">#${run.id}</span></h3>
            <div class="modal-body">${bodyHtml}</div>
            <div class="modal-actions">
                <button class="btn btn-secondary" data-action="close">Close</button>
            </div>
        </div>`;
    overlay.addEventListener('click', e => {
        if (e.target === overlay || e.target.getAttribute('data-action') === 'close')
            overlay.remove();
    });
    document.body.appendChild(overlay);
}

async function loadAdminData() {
    // Queue status
    const qResp = await fetch('/queue/state');
    if (qResp.ok) {
        renderQueueStatus(await qResp.json());
    }

    // Sync logs
    const sResp = await fetch('/admin/queue/sync-logs');
    if (sResp.ok) {
        const logs = await sResp.json();
        const el = document.getElementById('sync-logs');
        if (logs.length > 0) {
            el.innerHTML = `<table class="admin-table">
                <tr><th>Started</th><th>Status</th><th>New</th><th>Updated</th><th>Errors</th></tr>
                ${logs.slice(0, 5).map(l => `
                    <tr>
                        <td>${formatLocalDateTime(l.started_at)}</td>
                        <td>${l.status || ''}</td>
                        <td>${l.items_new || 0}</td>
                        <td>${l.items_updated || 0}</td>
                        <td>${l.errors || 0}</td>
                    </tr>
                `).join('')}
            </table>`;
        } else {
            el.innerHTML = '<p>No sync logs yet</p>';
        }
    }
}

// Render the live queue-status block. Shared by the initial load and the
// WebSocket so the count + now-playing stay current without a reload.
function renderQueueStatus(state) {
    const el = document.getElementById('queue-status');
    if (!el) return;
    const np = state && state.now_playing;
    el.innerHTML = `
        <p>Items in queue: ${((state && state.items) || []).length}</p>
        <p>Now playing: ${np ? escapeHtml(np.title || 'Unknown') : 'Nothing'}</p>
    `;
}

// Subscribe to the same /ws feed the public queue page uses, so queue size and
// now-playing update live. Jobs are DB-polled (below) since they aren't
// broadcast.
let adminWs = null;
let adminWsReconnect = null;
function connectAdminWebSocket() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    adminWs = new WebSocket(`${proto}//${location.host}/ws`);
    adminWs.onmessage = (event) => {
        let msg;
        try { msg = JSON.parse(event.data); } catch (e) { return; }
        if (msg.type === 'queue_state' || msg.type === 'queue_update') {
            renderQueueStatus(msg.data);
        } else if (msg.type === 'schedule_fired') {
            showToast(`Scheduled playlist loaded: ${msg.data.playlist_name}`);
            loadJobs();
        }
    };
    adminWs.onclose = () => {
        adminWsReconnect = setTimeout(connectAdminWebSocket, 3000);
    };
    setInterval(() => {
        if (adminWs && adminWs.readyState === WebSocket.OPEN) {
            adminWs.send(JSON.stringify({type: 'ping'}));
        }
    }, 30000);
}

// Summarize a job_runs.detail JSON blob for the history table.
function summarizeRunDetail(detail) {
    if (!detail) return '';
    let d;
    try { d = typeof detail === 'string' ? JSON.parse(detail) : detail; }
    catch { return String(detail).slice(0, 160); }
    if (d && d.error) return d.error;
    if (d && typeof d === 'object') {
        // Pipeline in-progress heartbeat
        if (d.step && typeof d.processed === 'number') {
            const bits = [`${d.step}: ${d.processed} processed`];
            if (d.changed) bits.push(`${d.changed} changed`);
            if (d.failed) bits.push(`${d.failed} failed`);
            return '\u23f3 ' + bits.join(', ');
        }
        // Pipeline final report
        if (d.steps_run && d.steps) {
            const tag = d.dry_run ? ' [dry run]' : '';
            const elapsed = d.elapsed_sec ? ` (${d.elapsed_sec}s)` : '';
            const parts = d.steps_run.filter(s => d.steps[s] && d.steps[s].processed).map(s => {
                const r = d.steps[s];
                const bits = [];
                if (r.changed) bits.push(`${r.changed}\u2713`);
                if (r.failed) bits.push(`${r.failed}\u2717`);
                return `${s}: ${bits.join(' ') || r.processed}`;
            });
            return parts.join(' \u00b7 ') + elapsed + tag;
        }
        const parts = [];
        if (d.sheet) parts.push(d.sheet);
        if (d.imported_playlists && d.imported_playlists.length) parts.push(`imported: ${d.imported_playlists.join(', ')}`);
        if (typeof d.resolved === 'number') parts.push(`resolved ${d.resolved}`);
        if (typeof d.failures === 'number' && d.failures) parts.push(`${d.failures} failed`);
        if (d.failures_detail && d.failures_detail.length) {
            const f = d.failures_detail[0];
            parts.push(`e.g. [${f.section || '?'} row ${f.row || '?'}] ${f.note || ''}`.trim());
        }
        if (d.played_movies && typeof d.played_movies.added === 'number' && d.played_movies.added > 0) {
            parts.push(`+${d.played_movies.added} played`);
        }
        if (typeof d.committed === 'number') parts.push(`committed ${d.committed}`);
        if (typeof d.total === 'number' && d.resolved !== undefined) {
            parts.push(`posters: ${d.resolved}/${d.total} resolved`);
            if (d.placeholder_used) parts.push(`${d.placeholder_used} placeholder`);
            if (d.failed) parts.push(`${d.failed} failed`);
            if (d.output_path) parts.push(`→ ${d.output_path}`);
        }
        if (typeof d.added_to_playlist !== 'undefined' && d.added_to_playlist) parts.push(`→ playlist ${d.added_to_playlist}`);
        if (parts.length) return parts.join(' · ');
    }
    return '';
}

// ── Feedback & suggestion triage queues ────────────────────────
function setBadge(elId, count) {
    const el = document.getElementById(elId);
    if (!el) return;
    if (count && count > 0) { el.textContent = count; el.hidden = false; }
    else { el.hidden = true; }
}

async function loadQueueBadges() {
    // Populate the Feedback/Suggestions "new" badges without opening the tabs.
    try {
        const [f, s] = await Promise.all([
            fetch('/admin/feedback?status=new'),
            fetch('/admin/suggestions?status=new'),
        ]);
        if (f.ok) { const d = await f.json(); setBadge('feedback-badge', d.counts && d.counts.new); }
        if (s.ok) { const d = await s.json(); setBadge('suggestions-badge', d.counts && d.counts.new); }
    } catch (e) { /* badges are best-effort */ }
}

function feedbackUnreadOnly() { return document.getElementById('feedback-unread-only').checked; }

async function loadFeedback() {
    const el = document.getElementById('feedback-list');
    el.innerHTML = 'Loading…';
    let resp;
    try { resp = await fetch('/admin/feedback' + (feedbackUnreadOnly() ? '?status=new' : '')); }
    catch (e) { el.innerHTML = '<p class="empty-state">Failed to load</p>'; return; }
    if (!resp.ok) { el.innerHTML = '<p class="empty-state">Failed to load</p>'; return; }
    const data = await resp.json();
    setBadge('feedback-badge', data.counts && data.counts.new);
    renderFeedback(data.items || []);
}

function renderFeedback(items) {
    const el = document.getElementById('feedback-list');
    if (!items.length) { el.innerHTML = '<p class="empty-state">No feedback.</p>'; return; }
    el.innerHTML = items.map(f => {
        const unread = f.status === 'new';
        return `<div class="queue-entry ${unread ? 'is-unread' : ''}">
            <div class="qe-head">
                <span class="qe-user">${escapeHtml(f.username || '')}</span>
                <span class="qe-time">${formatLocalDateTime(f.created_at)}</span>
                ${unread ? '<span class="qe-new">NEW</span>' : ''}
            </div>
            <div class="qe-body">${escapeHtml(f.body || '')}</div>
            <div class="qe-actions">
                <button class="btn btn-sm" onclick="markFeedback(${f.id}, ${unread})">${unread ? 'Mark read' : 'Mark unread'}</button>
                <button class="btn btn-sm btn-danger" onclick="deleteFeedback(${f.id})">Delete</button>
            </div>
        </div>`;
    }).join('');
}

async function markFeedback(id, read) {
    const resp = await fetch(`/admin/feedback/${id}/status`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: read ? 'read' : 'new' }),
    });
    if (resp.ok) loadFeedback(); else showToast('Update failed', 'error');
}

async function deleteFeedback(id) {
    if (!confirm('Delete this feedback?')) return;
    const resp = await fetch(`/admin/feedback/${id}`, { method: 'DELETE' });
    if (resp.ok) loadFeedback(); else showToast('Delete failed', 'error');
}

function suggestionsUnreadOnly() { return document.getElementById('suggestions-unread-only').checked; }

async function loadSuggestions() {
    const el = document.getElementById('suggestions-list');
    el.innerHTML = 'Loading…';
    let resp;
    try { resp = await fetch('/admin/suggestions' + (suggestionsUnreadOnly() ? '?status=new' : '')); }
    catch (e) { el.innerHTML = '<p class="empty-state">Failed to load</p>'; return; }
    if (!resp.ok) { el.innerHTML = '<p class="empty-state">Failed to load</p>'; return; }
    const data = await resp.json();
    setBadge('suggestions-badge', data.counts && data.counts.new);
    renderSuggestions(data.items || []);
}

function resolutionBadge(r) {
    if (r === 'already_have') return '<span class="res-badge res-have">Already in catalog</span>';
    if (r === 'resolved') return '<span class="res-badge res-ok">Matched</span>';
    return '<span class="res-badge res-unresolved">Unresolved</span>';
}

function renderSuggestions(items) {
    const el = document.getElementById('suggestions-list');
    if (!items.length) { el.innerHTML = '<p class="empty-state">No suggestions.</p>'; return; }
    el.innerHTML = items.map(s => {
        const unread = s.status === 'new';
        const year = s.resolved_year ? ` (${escapeHtml(s.resolved_year)})` : '';
        const src = (s.resolved_source || '').toLowerCase();
        const srcBadge = src ? ` <span class="cand-source cand-source-${escapeHtml(src)}">${escapeHtml(src.toUpperCase())}</span>` : '';
        const matched = s.resolved_title
            ? `<div class="qe-matched">Matched: <strong>${escapeHtml(s.resolved_title)}${year}</strong>${srcBadge}</div>`
            : '';
        const link = s.catalog_token
            ? `<a class="btn btn-sm" href="/catalog/item/${encodeURIComponent(s.catalog_token)}" target="_blank" rel="noopener">View in catalog ↗</a>`
            : '';
        return `<div class="queue-entry ${unread ? 'is-unread' : ''}">
            <div class="qe-head">
                <span class="qe-user">${escapeHtml(s.username || '')}</span>
                <span class="qe-time">${formatLocalDateTime(s.created_at)}</span>
                ${resolutionBadge(s.resolution)}
                ${unread ? '<span class="qe-new">NEW</span>' : ''}
            </div>
            <div class="qe-body">“${escapeHtml(s.query || '')}”</div>
            ${matched}
            <div class="qe-actions">
                ${link}
                <button class="btn btn-sm" onclick="markSuggestion(${s.id}, ${unread})">${unread ? 'Mark read' : 'Mark unread'}</button>
                <button class="btn btn-sm btn-danger" onclick="deleteSuggestion(${s.id})">Delete</button>
            </div>
        </div>`;
    }).join('');
}

async function markSuggestion(id, read) {
    const resp = await fetch(`/admin/suggestions/${id}/status`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: read ? 'read' : 'new' }),
    });
    if (resp.ok) loadSuggestions(); else showToast('Update failed', 'error');
}

async function deleteSuggestion(id) {
    if (!confirm('Delete this suggestion?')) return;
    const resp = await fetch(`/admin/suggestions/${id}`, { method: 'DELETE' });
    if (resp.ok) loadSuggestions(); else showToast('Delete failed', 'error');
}

// ── Tabs (lazy-load feedback/suggestions on first view) ────────
const adminTabLoaded = { overview: true, jobs: true, sync: true, feedback: false, suggestions: false, moderation: false };
function loadAdminTabOnce(tab) {
    if (adminTabLoaded[tab]) return;
    adminTabLoaded[tab] = true;
    if (tab === 'feedback') loadFeedback();
    else if (tab === 'suggestions') loadSuggestions();
    else if (tab === 'moderation') loadModerationTab();
}
function showAdminTab(tab) {
    document.querySelectorAll('.admin-tabs-card .tab-btn').forEach(b => {
        const on = b.dataset.tab === tab;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.admin-tabs-card .tab-panel').forEach(p => {
        p.hidden = p.id !== `tab-${tab}`;
    });
    loadAdminTabOnce(tab);
}
document.querySelectorAll('.admin-tabs-card .tab-btn').forEach(b => {
    b.addEventListener('click', () => showAdminTab(b.dataset.tab));
});
document.getElementById('feedback-refresh').addEventListener('click', loadFeedback);
document.getElementById('feedback-unread-only').addEventListener('change', loadFeedback);
document.getElementById('suggestions-refresh').addEventListener('click', loadSuggestions);
document.getElementById('suggestions-unread-only').addEventListener('change', loadSuggestions);

// ── Moderation tab ──────────────────────────────────────────────────────────
async function loadModerationTab() {
    await Promise.all([loadModStatus(), loadModEntries(), loadModPatterns()]);
}

// -- Service status --
async function loadModStatus() {
    const el = document.getElementById('mod-status-content');
    el.innerHTML = 'Loading…';
    try {
        const resp = await fetch('/admin/moderation/status');
        if (!resp.ok) { el.innerHTML = '<p class="empty-state">Failed to load status</p>'; return; }
        renderModStatus(el, await resp.json());
    } catch (e) { el.innerHTML = '<p class="empty-state">Failed to load status</p>'; }
}

function renderModStatus(el, data) {
    const p = data.ping || {};
    const h = data.health || {};
    const s = data.stats || {};
    const ok = h.status === 'healthy' || p.pong === true;
    const statusColor = ok ? 'var(--success)' : 'var(--danger)';
    function fmt(val) { return val != null ? escapeHtml(String(val)) : '—'; }
    function fmtUptime(sec) {
        if (sec == null) return '—';
        const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60);
        return hh ? `${hh}h ${mm}m` : `${mm}m`;
    }
    const version = h.version || p.version;
    const uptime = h.uptime_seconds != null ? h.uptime_seconds : p.uptime_seconds;
    el.innerHTML = `<div class="mod-status-grid">
        <div class="mod-stat">
            <span class="mod-stat-label">Status</span>
            <span class="mod-stat-value" style="color:${statusColor}">${ok ? 'healthy' : (h.status ? escapeHtml(h.status) : 'unknown')}</span>
        </div>
        <div class="mod-stat">
            <span class="mod-stat-label">Version</span>
            <span class="mod-stat-value">${fmt(version)}</span>
        </div>
        <div class="mod-stat">
            <span class="mod-stat-label">Uptime</span>
            <span class="mod-stat-value">${fmtUptime(uptime)}</span>
        </div>
        <div class="mod-stat">
            <span class="mod-stat-label">Users tracked</span>
            <span class="mod-stat-value">${fmt(s.users_tracked)}</span>
        </div>
        <div class="mod-stat">
            <span class="mod-stat-label">Bans enforced</span>
            <span class="mod-stat-value">${fmt(s.bans_enforced)}</span>
        </div>
        <div class="mod-stat">
            <span class="mod-stat-label">Mutes enforced</span>
            <span class="mod-stat-value">${fmt(s.mutes_enforced)}</span>
        </div>
    </div>`;
}

document.getElementById('mod-status-refresh').addEventListener('click', loadModStatus);

// -- Moderation entries --
let _modEntriesFilter = '';

async function loadModEntries() {
    const el = document.getElementById('mod-entries-list');
    el.innerHTML = 'Loading…';
    try {
        const qs = _modEntriesFilter ? `?filter=${encodeURIComponent(_modEntriesFilter)}` : '';
        const resp = await fetch(`/admin/moderation/entries${qs}`);
        if (!resp.ok) { el.innerHTML = '<p class="empty-state">Failed to load</p>'; return; }
        renderModEntries(el, (await resp.json()).entries || []);
    } catch (e) { el.innerHTML = '<p class="empty-state">Failed to load</p>'; }
}

function renderModEntries(el, entries) {
    if (!entries.length) { el.innerHTML = '<p class="empty-state">No moderation entries.</p>'; return; }
    el.innerHTML = `<table class="admin-table">
        <tr><th>Username</th><th>Action</th><th>Reason</th><th>Moderator</th><th>Added</th><th></th></tr>
        ${entries.map(e => `<tr>
            <td>${escapeHtml(e.username || '')}</td>
            <td><span class="mod-action mod-action-${escapeHtml(e.action || '')}">${escapeHtml(e.action || '')}</span></td>
            <td>${escapeHtml(e.reason || '—')}</td>
            <td>${escapeHtml(e.moderator || '—')}</td>
            <td>${e.added_at ? formatLocalDateTime(e.added_at) : '—'}</td>
            <td><button class="btn btn-sm btn-danger" data-mod-rm-username="${escapeHtml(e.username)}">Remove</button></td>
        </tr>`).join('')}
    </table>`;
}

async function removeModEntry(username) {
    if (!confirm(`Remove moderation for "${username}"?`)) return;
    const resp = await fetch(`/admin/moderation/entries/${encodeURIComponent(username)}`, { method: 'DELETE' });
    showToast(resp.ok ? `Removed ${username}` : 'Failed to remove', resp.ok ? 'success' : 'error');
    if (resp.ok) loadModEntries();
}

async function submitAddModEntry() {
    const username = document.getElementById('mod-add-username').value.trim();
    const action = document.getElementById('mod-add-action').value;
    const reason = document.getElementById('mod-add-reason').value.trim();
    if (!username) { showToast('Username required', 'error'); return; }
    const resp = await fetch('/admin/moderation/entries', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, action, reason: reason || null }),
    });
    if (resp.ok) {
        showToast(`${action} applied to ${username}`, 'success');
        document.getElementById('mod-add-username').value = '';
        document.getElementById('mod-add-reason').value = '';
        loadModEntries();
    } else {
        const d = await resp.json().catch(() => ({}));
        showToast(d.detail || 'Failed', 'error');
    }
}

document.getElementById('mod-entries-refresh').addEventListener('click', loadModEntries);

document.getElementById('mod-entries-list').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-mod-rm-username]');
    if (btn) removeModEntry(btn.dataset.modRmUsername);
});
document.querySelectorAll('.mod-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.mod-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        _modEntriesFilter = btn.dataset.filter;
        loadModEntries();
    });
});

// -- Patterns --
async function loadModPatterns() {
    const el = document.getElementById('mod-patterns-list');
    el.innerHTML = 'Loading…';
    try {
        const resp = await fetch('/admin/moderation/patterns');
        if (!resp.ok) { el.innerHTML = '<p class="empty-state">Failed to load</p>'; return; }
        renderModPatterns(el, (await resp.json()).patterns || []);
    } catch (e) { el.innerHTML = '<p class="empty-state">Failed to load</p>'; }
}

function renderModPatterns(el, patterns) {
    if (!patterns.length) { el.innerHTML = '<p class="empty-state">No patterns configured.</p>'; return; }
    el.innerHTML = `<table class="admin-table">
        <tr><th>Pattern</th><th>Regex</th><th>Action</th><th>Description</th><th>Added by</th><th></th></tr>
        ${patterns.map(p => `<tr>
            <td><code>${escapeHtml(p.pattern || '')}</code></td>
            <td>${p.is_regex ? '✓' : ''}</td>
            <td><span class="mod-action mod-action-${escapeHtml(p.action || '')}">${escapeHtml(p.action || '')}</span></td>
            <td>${escapeHtml(p.description || '—')}</td>
            <td>${escapeHtml(p.added_by || '—')}</td>
            <td><button class="btn btn-sm btn-danger" onclick="removeModPattern(${JSON.stringify(p.pattern)})">Remove</button></td>
        </tr>`).join('')}
    </table>`;
}

async function removeModPattern(pattern) {
    if (!confirm(`Remove pattern "${pattern}"?`)) return;
    const resp = await fetch(`/admin/moderation/patterns/${encodeURIComponent(pattern)}`, { method: 'DELETE' });
    showToast(resp.ok ? 'Pattern removed' : 'Failed to remove', resp.ok ? 'success' : 'error');
    if (resp.ok) loadModPatterns();
}

async function submitAddModPattern() {
    const pattern = document.getElementById('mod-pat-pattern').value.trim();
    const isRegex = document.getElementById('mod-pat-regex').checked;
    const action = document.getElementById('mod-pat-action').value;
    const description = document.getElementById('mod-pat-desc').value.trim();
    if (!pattern) { showToast('Pattern required', 'error'); return; }
    const resp = await fetch('/admin/moderation/patterns', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pattern, is_regex: isRegex, action, description: description || null }),
    });
    if (resp.ok) {
        showToast('Pattern added', 'success');
        document.getElementById('mod-pat-pattern').value = '';
        document.getElementById('mod-pat-desc').value = '';
        document.getElementById('mod-pat-regex').checked = false;
        loadModPatterns();
    } else {
        const d = await resp.json().catch(() => ({}));
        showToast(d.detail || 'Failed', 'error');
    }
}

document.getElementById('mod-patterns-refresh').addEventListener('click', loadModPatterns);

// Delegated click listener for the "Moderate…" buttons in the recent-users
// table. The table is rebuilt on each load so we delegate from the stable
// container div instead of attaching per-row handlers.
document.getElementById('mod-recent-list').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-mod-username]');
    if (btn) openModUserModal(btn.dataset.modUsername);
});

// -- Recent users --
async function loadRecentUsers() {
    const el = document.getElementById('mod-recent-list');
    const minutes = parseFloat(document.getElementById('mod-recent-window').value) || 60;
    el.innerHTML = 'Loading…';
    try {
        const resp = await fetch(`/admin/moderation/recent?window_minutes=${encodeURIComponent(minutes)}`);
        if (!resp.ok) { el.innerHTML = '<p class="empty-state">Failed to load</p>'; return; }
        renderRecentUsers(el, (await resp.json()).users || []);
    } catch (e) { el.innerHTML = '<p class="empty-state">Failed to load</p>'; }
}

function renderRecentUsers(el, users) {
    if (!users.length) { el.innerHTML = '<p class="empty-state">No recent users in this window.</p>'; return; }
    el.innerHTML = `<table class="admin-table">
        <tr><th>Username</th><th>First seen</th><th>Last seen</th><th>Sessions</th><th>Status</th><th></th></tr>
        ${users.map(u => {
            const modBadge = u.moderation_action
                ? `<span class="mod-action mod-action-${escapeHtml(u.moderation_action)}">${escapeHtml(u.moderation_action)}</span>`
                : '<span class="muted">—</span>';
            const actionBtn = u.moderation_action
                ? ''
                : `<button class="btn btn-sm" data-mod-username="${escapeHtml(u.username)}">Moderate…</button>`;
            return `<tr>
                <td>${escapeHtml(u.username || '')}</td>
                <td>${u.first_seen ? formatLocalDateTime(u.first_seen) : '—'}</td>
                <td>${u.last_seen ? formatLocalDateTime(u.last_seen) : '—'}</td>
                <td>${u.session_count != null ? u.session_count : '—'}</td>
                <td>${modBadge}</td>
                <td>${actionBtn}</td>
            </tr>`;
        }).join('')}
    </table>`;
}

function openModUserModal(username) {
    const existing = document.getElementById('mod-user-modal');
    if (existing) existing.remove();
    const overlay = document.createElement('div');
    overlay.id = 'mod-user-modal';
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-box" role="dialog" aria-modal="true">
            <h3>Moderate: ${escapeHtml(username)}</h3>
            <label class="field"><span>Action</span>
                <select id="mod-user-action">
                    <option value="ban">Ban</option>
                    <option value="smute">Soft mute (smute)</option>
                    <option value="mute">Mute</option>
                </select>
            </label>
            <label class="field"><span>Reason (optional)</span>
                <input type="text" id="mod-user-reason" placeholder="e.g. spamming">
            </label>
            <div class="modal-actions">
                <button class="btn btn-secondary" data-action="cancel">Cancel</button>
                <button class="btn btn-danger" data-action="apply">Apply</button>
            </div>
        </div>`;
    overlay.addEventListener('click', async (e) => {
        if (e.target === overlay) { overlay.remove(); return; }
        const action = e.target.getAttribute('data-action');
        if (action === 'cancel') { overlay.remove(); return; }
        if (action === 'apply') await _submitModUser(username, overlay);
    });
    document.body.appendChild(overlay);
    document.getElementById('mod-user-reason').focus();
}

async function _submitModUser(username, overlay) {
    const action = document.getElementById('mod-user-action').value;
    const reason = document.getElementById('mod-user-reason').value.trim();
    overlay.remove();
    const resp = await fetch('/admin/moderation/entries', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, action, reason: reason || null }),
    });
    if (resp.ok) {
        showToast(`${action} applied to ${username}`, 'success');
        loadRecentUsers();
        loadModEntries();
    } else {
        const d = await resp.json().catch(() => ({}));
        showToast(d.detail || 'Failed', 'error');
    }
}

loadAdminData();
loadJobs();
loadQueueBadges();
connectAdminWebSocket();

// Jobs are DB-polled (not broadcast), so refresh them periodically while the
// tab is visible to reflect running/finished status without a reload.
setInterval(() => {
    if (document.visibilityState === 'visible') loadJobs();
}, 5000);
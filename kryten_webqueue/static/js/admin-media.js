/* Admin media library: upload art and copy its permanent public URL. */

let mediaSearchTimer = null;

function copyText(text) {
    navigator.clipboard.writeText(text).then(() => showToast('Copied', 'success'));
}

function mediaCard(a) {
    const size = a.bytes >= 1048576 ? `${(a.bytes / 1048576).toFixed(1)} MB` : `${Math.ceil(a.bytes / 1024)} KB`;
    return `
        <div class="media-card">
            <a href="${escapeHtml(a.url)}" target="_blank" rel="noopener"><img src="${escapeHtml(a.url)}" alt="" loading="lazy"></a>
            <div class="motd-slot-title">${escapeHtml(a.slug)}</div>
            <div class="muted">${a.width}×${a.height} · ${size}${a.animated ? ` · ${a.frame_count} frames` : ''}</div>
            ${a.description ? `<div class="muted">${escapeHtml(a.description)}</div>` : ''}
            <div class="muted">${escapeHtml(a.uploaded_by)} · ${escapeHtml((a.uploaded_at || '').slice(0, 10))}</div>
            <input type="text" readonly value="${escapeHtml(a.url)}" class="media-url">
            <div class="motd-actions">
                <button type="button" class="btn btn-sm" data-copy="${escapeHtml(a.url)}">Copy URL</button>
                <button type="button" class="btn btn-sm" data-copy="${escapeHtml(a.snippet)}">Copy template snippet</button>
                <button type="button" class="btn btn-sm btn-danger" data-delete="${escapeHtml(a.slug)}">Delete</button>
            </div>
        </div>`;
}

async function loadMedia() {
    const q = document.getElementById('media-search').value.trim();
    const resp = await fetch(`/admin/media/assets?limit=200${q ? `&q=${encodeURIComponent(q)}` : ''}`);
    const list = document.getElementById('media-list');
    if (!resp.ok) { list.innerHTML = '<p class="empty-state">Failed to load the library.</p>'; return; }
    const { assets } = await resp.json();
    list.innerHTML = assets.length ? assets.map(mediaCard).join('') : '<p class="muted">Nothing here yet.</p>';
    list.querySelectorAll('[data-copy]').forEach(b => b.addEventListener('click', () => copyText(b.dataset.copy)));
    list.querySelectorAll('.media-url').forEach(i => i.addEventListener('focus', () => i.select()));
    list.querySelectorAll('[data-delete]').forEach(b => b.addEventListener('click', () => deleteMedia(b.dataset.delete)));
}

async function deleteMedia(slug) {
    if (!confirm(`Delete ${slug}? Its URL stops working immediately, including anywhere it was pasted outside MOTD templates.`)) return;
    const resp = await fetch(`/admin/media/assets/${encodeURIComponent(slug)}`, { method: 'DELETE' });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        const d = data.detail;
        showToast(d && d.templates ? `${d.message}: ${d.templates.join(', ')}` : (d || 'Delete failed'), 'error');
        return;
    }
    showToast('Deleted', 'success');
    loadMedia();
}

async function uploadMedia(e) {
    e.preventDefault();
    const file = document.getElementById('media-file').files[0];
    if (!file) { showToast('Choose an image first', 'error'); return; }
    const form = new FormData();
    form.append('file', file);
    const slug = document.getElementById('media-slug').value.trim();
    const desc = document.getElementById('media-desc').value.trim();
    if (slug) form.append('slug', slug);
    if (desc) form.append('description', desc);
    const resp = await fetch('/admin/media/assets', { method: 'POST', body: form });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) { showToast(data.detail || 'Upload failed', 'error'); return; }
    document.getElementById('media-result').innerHTML = `
        <p>Uploaded. Public link:</p>
        <div class="motd-actions">
            <input type="text" readonly value="${escapeHtml(data.url)}" class="media-url" id="media-new-url">
            <button type="button" class="btn btn-sm btn-primary" id="media-new-copy">Copy URL</button>
            <button type="button" class="btn btn-sm" id="media-new-snippet">Copy template snippet</button>
        </div>`;
    document.getElementById('media-new-copy').addEventListener('click', () => copyText(data.url));
    document.getElementById('media-new-snippet').addEventListener('click', () => copyText(data.snippet));
    document.getElementById('media-upload').reset();
    document.getElementById('media-drop-label').textContent = 'Drop an image here or click to choose';
    loadMedia();
}

document.addEventListener('DOMContentLoaded', () => {
    const drop = document.getElementById('media-drop');
    const input = document.getElementById('media-file');
    const label = document.getElementById('media-drop-label');
    input.addEventListener('change', () => { label.textContent = input.files[0] ? input.files[0].name : label.textContent; });
    ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('dragging'); }));
    ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('dragging'); }));
    drop.addEventListener('drop', (e) => {
        if (e.dataTransfer.files.length) {
            input.files = e.dataTransfer.files;
            label.textContent = input.files[0].name;
            const slug = document.getElementById('media-slug');
            if (!slug.value) slug.placeholder = input.files[0].name.replace(/\.[^.]+$/, '');
        }
    });
    document.getElementById('media-upload').addEventListener('submit', uploadMedia);
    document.getElementById('media-search').addEventListener('input', () => {
        clearTimeout(mediaSearchTimer);
        mediaSearchTimer = setTimeout(loadMedia, 300);
    });
    loadMedia();
});

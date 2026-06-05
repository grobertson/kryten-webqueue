/* kryten-webqueue — Main JavaScript */

// Toast notification system
function showToast(message, type = 'success') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// Logout handler
document.addEventListener('DOMContentLoaded', () => {
    const logoutBtn = document.getElementById('logout-btn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            await fetch('/auth/logout', { method: 'POST' });
            window.location.href = '/auth/login';
        });
    }
});

// --- Time formatting (always in the browser's local timezone) ---

function formatLocalDateTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString([], {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit'
    });
}

function formatLocalTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// --- Shared queue actions (used by catalog browse and item detail) ---

async function queueItem(token) {
    try {
        const resp = await fetch('/queue/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ friendly_token: token, tier: 'queue' })
        });
        const data = await resp.json();
        showToast(resp.ok ? 'Added to queue!' : (data.detail || `Failed (${resp.status})`), resp.ok ? 'success' : 'error');
    } catch (e) {
        showToast(`Network error: ${e.message}`, 'error');
    }
}

async function playNext(token) {
    try {
        const resp = await fetch('/queue/playnext', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ friendly_token: token })
        });
        const data = await resp.json();
        showToast(resp.ok ? 'Playing next!' : (data.detail || `Failed (${resp.status})`), resp.ok ? 'success' : 'error');
    } catch (e) {
        showToast(`Network error: ${e.message}`, 'error');
    }
}

// Admin queue: prompt for how to resolve position, then submit the chosen mode.
function queueAsAdmin(token) {
    showAdminQueueModal(token);
}

async function submitAdminQueue(token, mode) {
    closeAdminQueueModal();
    if (mode === 'cancel') return;
    try {
        const resp = await fetch('/admin/queue/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ friendly_token: token, mode })
        });
        const data = await resp.json();
        if (resp.ok) {
            const extra = data.refunded ? ` (${data.refunded} refunded)` : '';
            showToast(`Queued as admin!${extra}`);
        } else {
            showToast(data.detail || `Failed (${resp.status})`, 'error');
        }
    } catch (e) {
        showToast(`Network error: ${e.message}`, 'error');
    }
}

function showAdminQueueModal(token) {
    closeAdminQueueModal();
    const overlay = document.createElement('div');
    overlay.id = 'admin-queue-modal';
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-box" role="dialog" aria-modal="true">
            <h3>Queue as Admin</h3>
            <p>How should this item be positioned?</p>
            <div class="modal-actions">
                <button class="btn btn-primary" data-mode="playnext_refund">Play next &amp; refund all pending</button>
                <button class="btn" data-mode="after_purchased">Play after all purchased items</button>
                <button class="btn btn-secondary" data-mode="cancel">Cancel</button>
            </div>
        </div>`;
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closeAdminQueueModal();
        const mode = e.target.getAttribute('data-mode');
        if (mode) submitAdminQueue(token, mode);
    });
    document.body.appendChild(overlay);
}

function closeAdminQueueModal() {
    const existing = document.getElementById('admin-queue-modal');
    if (existing) existing.remove();
}

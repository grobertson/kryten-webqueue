/* kryten-webqueue — Main JavaScript */

// --- Theme (light/dark) ---
// The initial theme is applied pre-paint by an inline script in base.html.
// Here we keep the toggle button's icon in sync and persist explicit choices.
function currentTheme() {
    return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}

function updateThemeToggle() {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    const dark = currentTheme() === 'dark';
    // Show the icon for the theme you'd switch TO.
    btn.textContent = dark ? '\u2600\uFE0F' : '\uD83C\uDF19';
    btn.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
    btn.title = btn.getAttribute('aria-label');
}

function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('wq_theme', theme); } catch (e) { /* ignore */ }
    updateThemeToggle();
}

function toggleTheme() {
    setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
}

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
    updateThemeToggle();
    const themeBtn = document.getElementById('theme-toggle');
    if (themeBtn) {
        themeBtn.addEventListener('click', toggleTheme);
    }

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

function formatZ(amount) {
    if (amount == null) return '—';
    return Number(amount).toLocaleString() + ' Z';
}

// Queue / Play Next now open a receipt-style confirmation modal first.
function queueItem(token) {
    showReceiptModal(token, 'queue');
}

function playNext(token) {
    showReceiptModal(token, 'playnext');
}

async function showReceiptModal(token, tier) {
    closeReceiptModal();
    const overlay = document.createElement('div');
    overlay.id = 'receipt-modal';
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-box receipt-box" role="dialog" aria-modal="true">
            <h3>${tier === 'playnext' ? 'Play Next' : 'Add to Queue'}</h3>
            <div class="receipt-body"><p class="receipt-loading">Calculating cost…</p></div>
        </div>`;
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closeReceiptModal();
        const action = e.target.getAttribute('data-action');
        if (action === 'cancel') closeReceiptModal();
        if (action === 'confirm') confirmQueueAction(token, tier);
    });
    document.body.appendChild(overlay);

    try {
        const resp = await fetch(
            `/queue/preview?friendly_token=${encodeURIComponent(token)}&tier=${encodeURIComponent(tier)}`,
            { credentials: 'same-origin' }
        );
        const data = await resp.json();
        if (!resp.ok) {
            renderReceiptError(data.detail || `Could not load cost (${resp.status})`);
            return;
        }
        renderReceipt(data, tier);
    } catch (e) {
        renderReceiptError(`Network error: ${e.message}`);
    }
}

function renderReceipt(data, tier) {
    const body = document.querySelector('#receipt-modal .receipt-body');
    if (!body) return;

    const unavailable = data.available === false;
    const discount = data.discount_amount || 0;
    const discountPct = data.discount_pct || 0;
    const insufficient = data.balance != null && data.cost_z != null && data.balance < data.cost_z;

    let warning = '';
    if (unavailable) {
        warning = `<p class="receipt-warning">${escapeHtml(receiptErrorText(data.error_code))}</p>`;
    }

    body.innerHTML = `
        <table class="receipt-table">
            <tr><th>Item</th><td>${escapeHtml(data.title || 'Unknown')}</td></tr>
            <tr><th>Price</th><td>${formatZ(data.base_cost)}</td></tr>
            ${discount > 0 ? `<tr class="receipt-discount"><th>Discount${discountPct ? ` (${discountPct}%)` : ''}</th><td>-${formatZ(discount)}</td></tr>` : ''}
            <tr class="receipt-total"><th>Total</th><td>${formatZ(data.cost_z)}</td></tr>
            <tr><th>Balance</th><td>${formatZ(data.balance)}</td></tr>
            <tr class="${insufficient ? 'receipt-negative' : ''}"><th>Balance after</th><td>${formatZ(data.balance_after)}</td></tr>
        </table>
        ${warning}
        <div class="modal-actions">
            <button class="btn btn-secondary" data-action="cancel">Cancel</button>
            <button class="btn btn-primary" data-action="confirm" ${unavailable ? 'disabled' : ''}>
                ${tier === 'playnext' ? 'Confirm Play Next' : 'Confirm Queue'}
            </button>
        </div>`;
}

function renderReceiptError(message) {
    const body = document.querySelector('#receipt-modal .receipt-body');
    if (!body) return;
    body.innerHTML = `
        <p class="receipt-warning">${escapeHtml(message)}</p>
        <div class="modal-actions">
            <button class="btn btn-secondary" data-action="cancel">Close</button>
        </div>`;
}

function receiptErrorText(code) {
    switch (code) {
        case 'insufficient_balance': return 'You do not have enough Z for this.';
        case 'cooldown_active': return 'You are on cooldown. Try again shortly.';
        case 'daily_limit_reached': return 'You have reached your daily queue limit.';
        case 'blackout_active': return 'Queuing is temporarily disabled.';
        default: return code ? `Unavailable: ${code}` : 'This item is currently unavailable.';
    }
}

function closeReceiptModal() {
    const existing = document.getElementById('receipt-modal');
    if (existing) existing.remove();
}

async function confirmQueueAction(token, tier) {
    closeReceiptModal();
    const url = tier === 'playnext' ? '/queue/playnext' : '/queue/add';
    const payload = tier === 'playnext'
        ? { friendly_token: token }
        : { friendly_token: token, tier: 'queue' };
    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(payload)
        });
        const data = await resp.json();
        const okMsg = tier === 'playnext' ? 'Playing next!' : 'Added to queue!';
        showToast(resp.ok ? okMsg : (data.detail || `Failed (${resp.status})`), resp.ok ? 'success' : 'error');
    } catch (e) {
        showToast(`Network error: ${e.message}`, 'error');
    }
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : str;
    return div.innerHTML;
}

// --- Watchlist (My List) ---

let _watchlistTokens = null; // null = not yet fetched

async function _loadWatchlistTokens() {
    if (_watchlistTokens !== null) return _watchlistTokens;
    try {
        const resp = await fetch('/user/watchlist', { credentials: 'same-origin' });
        const data = resp.ok ? await resp.json() : {};
        _watchlistTokens = new Set(data.tokens || []);
    } catch (e) {
        _watchlistTokens = new Set();
    }
    return _watchlistTokens;
}

function _applyWatchlistBtn(btn, inList) {
    if (inList) {
        btn.textContent = '\u2713 My List';
        btn.classList.add('btn-watchlist-active');
        btn.title = 'Remove from My List';
    } else {
        btn.textContent = '+ My List';
        btn.classList.remove('btn-watchlist-active');
        btn.title = 'Add to My List';
    }
}

async function initWatchlistButtons() {
    const btns = document.querySelectorAll('.btn-watchlist');
    if (!btns.length) return;
    const tokens = await _loadWatchlistTokens();
    btns.forEach(btn => _applyWatchlistBtn(btn, tokens.has(btn.dataset.token)));
}

async function toggleWatchlist(btn, token) {
    const tokens = await _loadWatchlistTokens();
    const inList = tokens.has(token);
    try {
        const resp = await fetch(`/user/watchlist/${encodeURIComponent(token)}`, {
            method: inList ? 'DELETE' : 'POST',
            credentials: 'same-origin',
        });
        if (resp.ok) {
            inList ? tokens.delete(token) : tokens.add(token);
            _applyWatchlistBtn(btn, !inList);
            showToast(inList ? 'Removed from My List' : 'Added to My List');
        } else {
            const data = await resp.json().catch(() => ({}));
            showToast(data.detail || 'Failed', 'error');
        }
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

// ---------- generic admin modal ----------
function showModal(html) {
    closeModal();
    const o = document.createElement('div');
    o.className = 'modal-overlay'; o.id = 'admin-modal';
    o.innerHTML = `<div class="modal-box">${html}</div>`;
    o.addEventListener('click', e => { if (e.target === o) closeModal(); });
    document.body.appendChild(o);
}
function closeModal() { const m = document.getElementById('admin-modal'); if (m) m.remove(); }

// ---------- Delete permanently ----------
async function deleteItem(token, title, after) {
    const msg = `PERMANENT DELETE\n\n` +
                `This will remove "${title}" from the catalog AND MediaCMS.\n` +
                `This action CANNOT be undone.\n\n` +
                `Are you absolutely sure?`;
    
    if (!confirm(msg)) return;
    
    try {
        const resp = await fetch(`/admin/catalog/${encodeURIComponent(token)}`, {
            method: 'DELETE',
            credentials: 'same-origin',
        });
        const data = await resp.json();
        
        if (resp.ok) {
            showToast(`Deleted: ${title}`);
            setTimeout(() => {
                if (after === 'reload') {
                    // Browse view: stay on the current search/filter results.
                    window.location.reload();
                } else {
                    // Detail view: the item's page no longer exists.
                    window.location.href = '/catalog/browse';
                }
            }, 1000);
        } else {
            showToast(data.detail || `Failed to delete (${resp.status})`, 'error');
        }
    } catch (e) {
        showToast(`Network error: ${e.message}`, 'error');
    }
}

// ---------- duration formatter ----------
function fmtDur(sec) {
    if (!sec && sec !== 0) return '';
    const m = Math.floor(sec / 60);
    const s = Math.round(sec % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
}

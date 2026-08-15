# Visual & UX Improvements Sprint — August 2026

**Author**: Senior Microservice Architect (Kryten)  
**Date**: 2026-08-14  
**Status**: Draft — Awaiting Review  
**Estimated Effort**: 1-2 days (split into 4 sorties)

---

## Executive Summary

This sprint addresses 11 visual/UX issues identified across kryten-webqueue's user-facing interface, focusing on accessibility (light mode contrast), visual consistency (layout grids, pill/badge sizing), typography improvements, and user affordances (clickable elements, form validation, confirmation dialogs).

**Key Outcomes**:
- Improved readability in light mode (WCAG AA+ compliance for interactive elements)
- Consistent grid-based layout for admin jobs panel
- Enhanced typography hierarchy with context-appropriate font stacks
- Protected destructive actions with confirmation dialogs
- Better user affordances (clickable queue items, disabled-state inputs, keyboard shortcuts)

---

## Scope and Non-Goals

### In Scope
1. Light mode contrast fixes (pills, badges, Race page text)
2. Admin jobs panel grid layout
3. Navbar title with Channel-Z Unicode branding
4. Admin-only "Delete Permanently" action on catalog items
5. Typography refresh (serif for descriptions, improved sizing)
6. Queue page: clickable item links to detail view
7. Queue page: larger "Now Playing" description text
8. Detail view: Back link spacing and styling
9. Larger, more legible pills/badges/chips throughout
10. Menu link weight and hover decoration
11. Login page UX (keyboard submit, disabled state, improved copy)

### Out of Scope
- Dark mode palette changes (focus is light mode fixes)
- Responsive/mobile layout changes (current breakpoints preserved)
- WebSocket reconnect/error UI (separate epic)
- Catalog enrichment pipeline UI (separate work)

---

## Problem Statement

Current pain points identified by the operator:

1. **Accessibility**: Light mode pills and Race page text fail contrast checks (WCAG AA), making them unreadable.
2. **Visual Clutter**: Admin jobs panel has ragged, inconsistent widths; looks unprofessional.
3. **Branding**: Channel-Z title in navbar lacks the distinctive Unicode box branding used elsewhere.
4. **Safety**: No guard rail for catalog item deletion — destructive action with no confirmation.
5. **Typography**: Generic sans-serif for all text; descriptions too small and lack serif readability.
6. **Affordance**: Queue items look clickable but aren't; users can't navigate to details from queue.
7. **Hierarchy**: "Now Playing" description is illegibly small (0.7rem) — defeats its purpose.
8. **Spacing**: Detail view Back button crowds the artwork; needs breathing room.
9. **Legibility**: Pills/badges (tags, people, studios) are too small (0.65-0.72rem) to read comfortably.
10. **Discoverability**: Menu links are lightweight and lack hover affordance.
11. **Form UX**: Login form doesn't respond to Enter key; submit button always active even when empty.

---

## Design

### Architecture

All changes are **front-end only** — CSS, HTML templates, and client-side JavaScript. No backend routes or database schema changes required, except:

- **New admin route**: `DELETE /admin/catalog/items/{friendly_token}` (deletion from SQLite + MediaCMS)
- **MediaCMS integration**: `DELETE https://www.dropsugar.co/api/v1/media/{friendly_token}` (returns 204 on success)

### Font Stack Strategy

Replace the current uniform Inter/Sora stack with context-appropriate fonts:

| Context | Font Stack | Rationale |
|---------|-----------|-----------|
| **Body text** (paragraphs, descriptions) | `Georgia, 'Times New Roman', serif` | Traditional serif for extended reading; high x-height |
| **Headings** (h1-h6, page titles) | `Georgia, 'Times New Roman', serif` | Traditional serif for hierarchy and elegance |
| **Menu/nav** | `'Inter', 'Helvetica Neue', 'Segoe UI', sans-serif` | Clean, high-legibility sans (weights: 400, 500, 600, 700) |
| **UI labels** (buttons, form labels, chips) | `'Inter', 'Helvetica Neue', 'Segoe UI', sans-serif` | Modern sans for UI chrome (weights: 400, 500, 600, 700) |
| **Logs/monospace** (sync logs, job output, code blocks) | `'Consolas', 'Courier New', monospace` | Monospace for structured data |

**Migration Note**: Remove Google Fonts `Sora` dependency; use `Inter` with full weight range (400, 500, 600, 700) + web-safe fallbacks. Georgia serif for body and headings. Update `base.html` and CSS root variables.

### Component Changes

#### 1. Light Mode Contrast (Sortie 1)

**Problem**: Pills, badges, and Race page elements fail WCAG AA (4.5:1 for normal text, 3:1 for large/bold).

**Solution**:
- Audit all `.pill`, `.badge`, `.np-chip`, `.job-badge` classes in light mode
- Increase contrast ratios:
  - **Pills on Race page**: Change backgrounds to opaque colors with darker text
  - **Job badges**: Darken text color in light mode
  - **Category/tag chips**: Ensure border + text meet 4.5:1 against `--bg-card`
- Test with [WebAIM Contrast Checker](https://webaim.org/resources/contrastchecker/)

**Light Mode Overrides** (`:root[data-theme="light"]`):
```css
.pill-betting { background: #0969da; color: #ffffff; }
.pill-racing  { background: #1a7f37; color: #ffffff; }
.pill-finished{ background: #bf8700; color: #ffffff; }
.pill-idle    { background: #57606a; color: #ffffff; }

.job-badge { color: var(--text-primary); background: var(--bg-secondary); }

.np-chip-cat { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
.np-chip-tag { background: var(--warning); color: var(--bg-primary); border-color: var(--warning); }
/* etc. for person, studio, other badges */
```

**Race Page**: Move `.race-commentary`, `.lane`, and all `.pill-*` rules into a `<style>` block override in `race.html` for light mode.

#### 2. Admin Jobs Grid Layout (Sortie 1)

**Problem**: `.jobs-list` uses flex with variable widths; badges and timestamps don't align.

**Solution**: Replace `.job-row` flexbox with CSS Grid for consistent alignment:
```css
.job-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto auto;
    gap: 0.75rem;
    align-items: center;
    padding: 0.5rem 0.75rem;
    background: var(--bg-card);
    border-radius: var(--radius);
}
```

Grid columns:
1. **Job label** (flex, min-width 0 to prevent overflow)
2. **Status badge** (auto width)
3. **Last run timestamp** (auto width, tabular-nums)
4. **Action button** (auto width)

#### 3. Channel-Z Navbar Title (Sortie 1)

**Problem**: Navbar shows plain "Channel-Z" text.

**Solution**: Update `base.html` `.nav-brand` link with Unicode box branding:
```html
<a href="/catalog/browse">🟩🟨🟧🟥Channel-Z🟥🟧🟨🟩</a>
```

**CSS**: Ensure emoji render inline (no line breaks); add slight letter-spacing to prevent crowding:
```css
.nav-brand a {
    /* existing styles */
    white-space: nowrap;
    letter-spacing: 0.02em;
}
```

#### 4. Delete Permanently Button (Sortie 2)

**High-stakes change** — permanent deletion from catalog AND MediaCMS.

**UI**:
- Add button to catalog browse cards (`.catalog-card .card-actions`) when `user.rank >= 3`
- Add button to detail view actions (`.item-detail-actions`) when `user.rank >= 3`
- Button style: `.btn-danger` variant, smaller size (`.btn-sm`)
- Button icon/label: "🗑️ Delete" or just "Delete"

**Confirmation Dialog**:
- JavaScript `confirm()` with clear warning:
  ```js
  const msg = `PERMANENT DELETE\n\n` +
              `This will remove "${itemTitle}" from the catalog AND MediaCMS.\n` +
              `This action CANNOT be undone.\n\n` +
              `Are you absolutely sure?`;
  if (!confirm(msg)) return;
  ```

**Backend Route** (new):
```python
# kryten_webqueue/routes/admin_catalog.py

@router.delete("/admin/catalog/items/{friendly_token}")
async def delete_catalog_item(
    friendly_token: str,
    user: dict = Depends(require_admin)
):
    """
    Permanently delete a catalog item from SQLite and MediaCMS.
    Admin-only. No recovery path — use with caution.
    """
    # 1. Fetch item from catalog DB (need media_id for MediaCMS)
    item = await db.get_catalog_item(friendly_token)
    if not item:
        raise HTTPException(404, "Item not found")
    
    # 2. Delete from MediaCMS (if media_id exists)
    if item.get("media_id"):
        cms = MediaCMSClient(config)
        try:
            await cms.delete_media(item["media_id"])
        except Exception as e:
            logger.error(f"Failed to delete {friendly_token} from MediaCMS: {e}")
            # Continue — catalog deletion is the source of truth
    
    # 3. Delete from local catalog DB
    await db.delete_catalog_item(friendly_token)
    
    # 4. Delete enrichment state (if exists)
    enrich_state_path = Path(config.db_path).parent / "enrichment_state.json"
    if enrich_state_path.exists():
        # Remove from enrichment state dict (keyed by friendly_token)
        # (implementation detail — depends on enrichment state format)
    
    return {"success": True, "deleted": friendly_token}
```

**Database Method** (new):
```python
# kryten_webqueue/catalog/db/_operations.py

async def delete_catalog_item(friendly_token: str):
    """Permanently delete a catalog item and all facet associations."""
    async with get_db() as db:
        # Delete facet associations first (FK constraints)
        await db.execute(
            "DELETE FROM catalog_items_tags WHERE item_id = (SELECT id FROM catalog_items WHERE friendly_token = ?)",
            (friendly_token,)
        )
        await db.execute(
            "DELETE FROM catalog_items_categories WHERE item_id = (SELECT id FROM catalog_items WHERE friendly_token = ?)",
            (friendly_token,)
        )
        await db.execute(
            "DELETE FROM catalog_items_people WHERE item_id = (SELECT id FROM catalog_items WHERE friendly_token = ?)",
            (friendly_token,)
        )
        # Delete the item itself
        await db.execute("DELETE FROM catalog_items WHERE friendly_token = ?", (friendly_token,))
        await db.commit()
```

**MediaCMS Client** (new method):
```python
# kryten_webqueue/integrations/mediacms.py

async def delete_media(self, friendly_token: str) -> bool:
    """
    Delete a media item from MediaCMS.
    
    Args:
        friendly_token: The item's friendly_token (MediaCMS uses this as primary identifier)
    
    Returns:
        True if deletion succeeds (204 response), False otherwise
    
    DELETE https://www.dropsugar.co/api/v1/media/{friendly_token}
    Success: 204 No Content
    """
    url = f"{self.base_url}/api/v1/media/{friendly_token}"
    headers = {"Authorization": f"Token {self.api_token}"}
    
    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers=headers, timeout=10.0)
        if resp.status_code == 204:
            return True
        elif resp.status_code == 404:
            logger.warning(f"MediaCMS item {friendly_token} not found (already deleted?)")
            return True  # Idempotent
        else:
            logger.error(f"MediaCMS deletion failed for {friendly_token}: {resp.status_code} {resp.text}")
            return False
```

#### 5. Typography Refresh (Sortie 3)

**Changes**:
1. **Root CSS variables**: Update `--font-body` and `--font-heading` to use Georgia serif
2. **Body text**: Apply `font-family: var(--font-body)` (Georgia serif) to `<body>`, `.item-detail-description`, `.np-description`, paragraphs
3. **Headings**: Ensure h1-h6 use `var(--font-heading)` (Georgia serif) with appropriate weights (600-700)
4. **UI elements**: Keep Inter sans for nav, buttons, chips, labels with weight range (400-700)
5. **Monospace**: Add `.monospace` utility class for logs, apply to sync logs, job output
6. **Size increases**:
   - `.np-description`: `0.7rem → 0.95rem` (queue now-playing description)
   - `.item-detail-description`: keep `line-height: 1.6`, ensure readable size
   - `.qi-title` (queue item titles): `0.9rem → 1rem`
7. **Weight hierarchy**:
   - Menu links: 500 (medium)
   - Headings: 600-700 (semi-bold to bold)
   - Buttons: 500-600 (medium to semi-bold)
   - Body: 400 (regular)

**Font Loading** (base.html):
- Remove Sora from Google Fonts link
- Load Inter with full weight range for UI elements
- Georgia is web-safe (no CDN needed)
- Update `<link>` tag:
  ```html
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
  ```

#### 6. Queue Page Clickable Items (Sortie 3)

**Problem**: Queue items (`.queue-item`) aren't linked to detail view.

**Solution**: Wrap `.qi-title` and `.qi-cover` in `<a>` tags:
```html
<div class="queue-item">
    <a href="/catalog/items/{{ item.friendly_token }}" class="qi-cover-link">
        <div class="qi-cover"><!-- img or placeholder --></div>
    </a>
    <div class="qi-info">
        <a href="/catalog/items/{{ item.friendly_token }}" class="qi-title-link">
            <div class="qi-title">{{ item.title }}</div>
        </a>
        <!-- ... meta, duration, etc. -->
    </div>
    <!-- ... qi-right -->
</div>
```

**CSS**: Style links to remove underline, inherit text color, add hover state:
```css
.qi-cover-link,
.qi-title-link {
    text-decoration: none;
    color: inherit;
    display: block;
}
.qi-cover-link:hover,
.qi-title-link:hover {
    opacity: 0.8;
}
.qi-title-link .qi-title {
    transition: color 0.2s;
}
.qi-title-link:hover .qi-title {
    color: var(--accent);
}
```

**"Now Playing" card**: Same treatment for `.np-cover` and `.np-title`.

#### 7. Detail View Back Link Spacing (Sortie 3)

**Problem**: `.btn-back` crowds the artwork; button is too heavy for a nav link.

**Solution**:
1. **Change button to link**:
   ```html
   <a href="javascript:history.back()" class="back-link">&larr; Back</a>
   ```
2. **CSS**:
   ```css
   .back-link {
       display: inline-block;
       font-size: 0.9rem;
       color: var(--text-secondary);
       text-decoration: none;
       margin-bottom: 1.5rem;  /* breathing room below */
       margin-top: 0.5rem;      /* space above (reduce crowding with nav) */
       transition: color 0.2s;
   }
   .back-link:hover {
       color: var(--accent);
   }
   ```

#### 8. Larger Pills/Badges/Chips (Sortie 3)

**Problem**: Pills range from 0.65rem to 0.72rem — too small.

**Solution**: Increase all to **0.85-0.95rem** range:
- `.badge`, `.job-badge`: `0.65rem → 0.85rem`
- `.np-chip` (tags, categories, people, studios): `0.72rem → 0.9rem`
- `.qi-badge` (queue paid/promo badges): `0.7rem → 0.85rem`
- Race `.pill-*`: keep `0.82rem` (already larger; may increase to `0.9rem`)

**Padding adjustments**: Increase padding proportionally to maintain visual balance:
- `.badge`: `0.1rem 0.45rem → 0.2rem 0.6rem`
- `.np-chip`: `0.2rem 0.55rem → 0.3rem 0.7rem`

#### 9. Menu Link Weight and Hover (Sortie 4)

**Problem**: `.nav-links a` is lightweight (default `font-weight: 400`), no hover decoration.

**Solution**: Use Inter's weight range for hierarchy and add hover decoration
```css
.nav-links a {
    font-family: var(--font-ui);  /* Inter sans */
    font-weight: 500;  /* medium weight from Inter's range */
    position: relative;
    transition: color 0.2s;
}
.nav-links a:hover {
    color: var(--text-primary);
    font-weight: 600;  /* semi-bold on hover for emphasis */
}
/* Underline decoration on hover */
.nav-links a::after {
    content: '';
    position: absolute;
    bottom: -4px;
    left: 0;
    right: 0;
    height: 2px;
    background: var(--accent);
    transform: scaleX(0);
    transition: transform 0.2s ease;
}
.nav-links a:hover::after {
    transform: scaleX(1);
}
```

#### 10. Login Page UX (Sortie 4)

**Changes**:
1. **Title**: Replace "Login" with Channel-Z Unicode boxes logo
   ```html
   <h1 class="login-title">🟩🟨🟧🟥Channel-Z🟥🟧🟨🟩</h1>
   ```
   ```css
   .login-title {
       font-family: var(--font-heading);  /* Georgia serif */
       font-weight: 700;
       white-space: nowrap;
       letter-spacing: 0.02em;
   }
   ```
2. **Instructional text**: Increase weight and size
   ```html
   <p class="login-instructions">Enter your CyTube username to receive a one-time code via PM.</p>
   ```
   ```css
   .login-instructions {
       font-size: 1.1rem;
       font-weight: 600;  /* semi-bold for emphasis */
       margin-bottom: 0.75rem;
   }
   ```
3. **Enter key submit**: Add event listeners
   ```js
   document.getElementById('username').addEventListener('keypress', (e) => {
       if (e.key === 'Enter') document.getElementById('request-otp-btn').click();
   });
   document.getElementById('otp-code').addEventListener('keypress', (e) => {
       if (e.key === 'Enter') document.getElementById('verify-otp-btn').click();
   });
   ```
4. **Disabled button state**:
   ```js
   const usernameInput = document.getElementById('username');
   const requestBtn = document.getElementById('request-otp-btn');
   const otpInput = document.getElementById('otp-code');
   const verifyBtn = document.getElementById('verify-otp-btn');

   usernameInput.addEventListener('input', () => {
       requestBtn.disabled = !usernameInput.value.trim();
   });
   otpInput.addEventListener('input', () => {
       verifyBtn.disabled = !otpInput.value.trim();
   });

   // Initialize disabled state
   requestBtn.disabled = true;
   verifyBtn.disabled = true;
   ```
   ```css
   .btn:disabled {
       opacity: 0.5;
       cursor: not-allowed;
   }
   ```

---

## Implementation Plan

### Sortie Breakdown

#### **Sortie 1: Contrast Fixes & Admin Jobs Grid** (2-3 hours)
- Files: `main.css`, `admin/index.html`, `race.html`
- Light mode overrides for pills, badges, chips
- Grid layout for `.jobs-list`
- Channel-Z Unicode title in navbar (`base.html`)

#### **Sortie 2: Delete Permanently Feature** (3-4 hours)
- **High-stakes**: Backend route, database method, confirmation UX
- Files: `routes/admin_catalog.py`, `catalog/db/_operations.py`, `integrations/mediacms.py`, `templates/catalog/browse.html`, `templates/catalog/item_detail.html`, `static/js/main.js`
- Add deletion route + DB method
- Add buttons to browse and detail templates (admin-only)
- Confirmation dialog in JS
- Test deletion flow (catalog → MediaCMS)

#### **Sortie 3: Typography & Clickable Queue** (3-4 hours)
- Files: `base.html`, `main.css`, `templates/queue/index.html`, `templates/catalog/item_detail.html`
- Font stack updates (root variables + Google Fonts link)
- Increase `.np-description`, pill sizes
- Wrap queue item titles/covers in `<a>` tags
- Back link spacing and styling
- Monospace utility for logs

#### **Sortie 4: Menu & Login Polish** (2 hours)
- Files: `main.css`, `templates/auth/login.html`
- Menu link weight + hover decoration
- Login page copy, keyboard shortcuts, disabled states

**Total Estimated Effort**: 10-13 hours (1.5-2 days)

---

## Testing Strategy

### Manual Testing Checklist

**Light Mode Contrast**:
- [ ] Switch to light mode, verify pills on Race page are readable
- [ ] Admin jobs panel: badges pass contrast check
- [ ] Browse page: tags, categories, people chips are legible

**Admin Jobs Grid**:
- [ ] Jobs panel renders as aligned grid (badges, timestamps, buttons)
- [ ] Layout holds on narrow screens (no overflow)

**Channel-Z Title**:
- [ ] Navbar shows Unicode box branding
- [ ] Emoji render inline (no line breaks)

**Delete Permanently**:
- [ ] Button appears on browse tiles (admin only)
- [ ] Button appears on detail view (admin only)
- [ ] Confirmation dialog shows item title, warns of permanence
- [ ] Canceling confirmation aborts deletion
- [ ] Confirming deletion removes item from catalog DB
- [ ] Item removed from MediaCMS (verify in CMS UI)
- [ ] Deleted item no longer appears in browse/search
- [ ] Non-admin users don't see delete button

**Typography**:
- [ ] Descriptions use serif font (Georgia fallback)
- [ ] Headings use sans font (Inter/Helvetica)
- [ ] Sync logs use monospace
- [ ] `.np-description` is larger, more legible

**Clickable Queue**:
- [ ] Queue item titles link to detail view
- [ ] Queue item artwork links to detail view
- [ ] Hover state shows link affordance (color change, cursor)
- [ ] "Now Playing" title/artwork also clickable

**Detail View Spacing**:
- [ ] Back link has breathing room above/below
- [ ] Back link styled as lightweight nav element (not button)

**Larger Pills**:
- [ ] Tags, categories, people chips are 0.9rem+
- [ ] Job badges are 0.85rem+
- [ ] Queue paid/promo badges are 0.85rem+
- [ ] Pills are easier to read at-a-glance

**Menu Links**:
- [ ] Nav links have medium weight (500)
- [ ] Hover shows underline decoration
- [ ] Transition is smooth

**Login Page**:
- [ ] Title reads "Welcome to Channel-Z"
- [ ] Instructional text is heavier, larger
- [ ] Enter key submits username input
- [ ] Enter key submits OTP input
- [ ] Submit buttons disabled when inputs empty
- [ ] Buttons enable when text entered

### Automated Tests

**Unit Tests** (if applicable):
- `test_delete_catalog_item()` — verify DB deletion + MediaCMS call
- `test_delete_nonexistent_item()` — 404 handling
- `test_delete_requires_admin()` — authZ check

**Integration Tests**:
- Full deletion flow (POST → DELETE → verify item gone)

---

## Rollout

### Pre-Deployment

1. **Config check**: Ensure `mediacms_url`, `mediacms_token`, `mediacms_manage_all_media` are set
2. **Backup**: Snapshot catalog DB before deploying delete feature
3. **Dry run**: Test deletion on a non-prod MediaCMS instance (if available)

### Deployment Steps

1. Bump version in `pyproject.toml` (e.g., `0.37.0 → 0.38.0`)
2. Update `CHANGELOG.md`:
   ```markdown
   ## [0.38.0] - 2026-08-XX
   ### Added
   - Admin "Delete Permanently" button for catalog items (browse + detail views)
   - Confirmation dialog for destructive deletion (catalog + MediaCMS)
   
   ### Changed
   - Typography refresh: serif for descriptions, modern sans for headings/UI
   - Light mode contrast improvements (pills, badges, Race page)
   - Admin jobs panel now uses grid layout for alignment
   - Queue items (title, artwork) now link to detail view
   - Now Playing description text larger (0.7rem → 0.95rem)
   - Detail view Back button styled as link with better spacing
   - Pills/badges/chips increased to 0.85-0.95rem for legibility
   - Menu links heavier weight with hover underline decoration
   - Channel-Z navbar title includes Unicode box branding
   - Login page: "Welcome to Channel-Z" title, better instructions, keyboard submit, disabled button state
   ```
3. Commit, tag, push to `main`
4. Deploy via pipx on `grindhouse.local` (see kryten-webqueue AGENTS.md)
5. Monitor for errors in uvicorn logs

### Monitoring

- **Logs**: Watch for MediaCMS deletion failures (network, auth, 404)
- **User Reports**: Confirm light mode readability improvements
- **Catalog Integrity**: Spot-check that deleted items stay deleted (no resurrection via sync)

---

## Documentation

### Code Comments

- Document `delete_catalog_item()` route with **HIGH-STAKES** warning
- Document DB method with FK cascade notes
- Comment confirmation dialog logic in `main.js`

### User-Facing Docs

- Update `README.md` (if it mentions deletion/hiding workflow)
- Add operator note: "Delete Permanently" removes from MediaCMS — use Hide for soft-delete

### Architecture Docs

- `docs/IMPL_*.md`: Note deletion flow if applicable
- Update this spec's status to "Implemented" when complete

---

## Open Questions & Risks

### Questions
1. **MediaCMS API**: ✅ **RESOLVED** — `DELETE https://www.dropsugar.co/api/v1/media/{friendly_token}` returns 204 on success. Uses friendly_token (not media_id) as identifier.
2. **Enrichment State**: Current format is JSON file keyed by `friendly_token`? Confirm before implementing cleanup.
3. **Catalog Sync**: ✅ **RESOLVED** — Items deleted from MediaCMS will not resurrect on sync (source of truth is MediaCMS).
4. **Backup Strategy**: ✅ **APPROVED** — Implement soft-delete flag as safety measure before hard-delete goes live. Add `deleted_at` timestamp to catalog schema, filter from all views, allow 30-day recovery window with manual purge.

### Risks
- **Irreversible deletion**: No undo path (after 30-day soft-delete window). Mitigation: soft-delete flag + strong confirmation dialog, admin-only, DB backups.
- **MediaCMS orphans**: If MediaCMS deletion fails (network, auth, etc.) but catalog deletion succeeds, item is orphaned in CMS. Mitigation: log errors with friendly_token, manual cleanup script, idempotent retry.
- **Light mode regressions**: Changing contrast ratios may affect dark mode. Mitigation: test both themes before deploying.
- **Typography shift**: Serif fonts for headings and body may look jarring if users are accustomed to current sans-serif style. Mitigation: gather feedback in first 24h, be prepared to revert to Inter if needed.

---

## Success Metrics

- **Contrast**: All interactive elements in light mode pass WCAG AA (4.5:1 for text, 3:1 for large/bold)
- **Consistency**: Admin jobs panel shows aligned grid with no ragged edges
- **Branding**: Channel-Z title visually distinct in navbar
- **Safety**: Zero accidental deletions reported (confirmation dialog effective)
- **Readability**: Positive feedback on description text size and font choice
- **Engagement**: Users navigate to detail view from queue page (track analytics if available)

---

## Future Enhancements

- **Deletion history**: Log deleted items in a separate table for audit trail
- **Soft delete**: Add `deleted_at` timestamp, filter from browse, allow restore within 30 days
- **Bulk actions**: Select multiple items on browse page, delete in batch
- **MediaCMS sync guard**: Flag items as "intentionally deleted" to prevent re-sync
- **WCAG AAA**: Target 7:1 contrast for all text (current target is AA 4.5:1)
- **Font customization**: User preference for font stack (serif vs. sans for body)

---

## Appendix: Files Modified

### Templates
- `kryten_webqueue/templates/base.html` (navbar title, font loading)
- `kryten_webqueue/templates/auth/login.html` (title, instructions, keyboard, disabled state)
- `kryten_webqueue/templates/admin/index.html` (jobs grid structure)
- `kryten_webqueue/templates/race.html` (light mode pill overrides)
- `kryten_webqueue/templates/queue/index.html` (clickable items)
- `kryten_webqueue/templates/catalog/browse.html` (delete button)
- `kryten_webqueue/templates/catalog/item_detail.html` (delete button, back link)

### CSS
- `kryten_webqueue/static/css/main.css` (all typography, sizing, layout, contrast changes)

### JavaScript
- `kryten_webqueue/static/js/main.js` (delete confirmation, login UX)

### Backend
- `kryten_webqueue/routes/admin_catalog.py` (new DELETE route)
- `kryten_webqueue/catalog/db/_operations.py` (new `delete_catalog_item()` method)
- `kryten_webqueue/integrations/mediacms.py` (verify `delete_media()` signature)

### Docs
- `CHANGELOG.md` (release notes)
- This spec document

---

**End of Specification**

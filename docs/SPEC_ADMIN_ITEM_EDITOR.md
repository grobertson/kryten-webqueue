# Admin Manual Item Editor — Implementation Spec

**Version**: 1.0  
**Status**: Implementing  
**Date**: 2026-08-14

---

## Overview

Add an admin-only manual edit capability for catalog items, appearing on both list view tiles and item detail pages. This complements the existing re-enrich button by allowing admins to directly fix metadata that can't be automatically corrected.

## User Requirements

Based on user feedback:
1. **Include enrichment state editing in V1** — allow editing classification fields
2. **Include edit audit logging in V1** — track who edited what and when
3. **"Re-enrich on save"** — optional checkbox that triggers the standard enrichment job for the edited item
4. **Skip tag/category editing** — these are better managed on the MediaCMS side

---

## Architecture

### What Fields Are Editable?

**Core Metadata** (always editable):
- `title` — display title
- `description` — synopsis/overview
- `duration_sec` — runtime in seconds

**Enrichment State** (advanced):
- `content_type` — classification (movie, tv_episode, hosted_movie, riffed_movie, archive, unknown)
- `lookup_title` — normalized title for TMDB/OMDB search
- `lookup_year` — year for metadata lookup
- `hosted_show` — for hosted movies (Svengoolie, MST3K, JBB, TLDI, etc.)
- `tv_show`, `tv_season`, `tv_episode_num` — for TV episodes

### Sync Strategy

- **Title/Description → MediaCMS**: Optional via "Sync to MediaCMS" checkbox (default: checked)
- **Enrichment State**: Local catalog only (not a MediaCMS concern)
- **Re-enrich trigger**: Optional via "Re-enrich after save" checkbox — runs the standard `catalog_enrich` job for this single item

### Audit Trail

New `item_edit_log` table:
```sql
CREATE TABLE item_edit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    friendly_token TEXT NOT NULL,
    username TEXT NOT NULL,
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    edited_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

---

## Implementation Plan

### Files to Create

1. **`kryten_webqueue/templates/catalog/edit_item_modal.html`**
   - Modal dialog with form for editing
   - Pre-populated with current data
   - Two sections: Core Metadata + Advanced (enrichment state)

### Files to Modify

1. **Backend**
   - `kryten_webqueue/routes/admin_catalog.py`
     - Add `PATCH /admin/catalog/{friendly_token}/edit`
     - Validate, update catalog + enrichment state
     - Log changes to audit trail
     - Optionally sync to MediaCMS
     - Optionally trigger re-enrichment job

   - `kryten_webqueue/catalog/db/_connection.py`
     - Add migration v21: `item_edit_log` table

   - `kryten_webqueue/catalog/db/_enrichment.py`
     - Add `update_enrichment_state(token, fields)` method

   - `kryten_webqueue/catalog/db/_catalog.py`
     - Add `log_item_edit(token, username, field, old, new)` method
     - Add `get_item_edit_history(token, limit)` method

   - `kryten_webqueue/catalog/mediacms.py`
     - Add `update_item(token, title, description)` method

2. **Frontend**
   - `kryten_webqueue/templates/catalog/browse.html`
     - Add "Edit" button in admin actions
     - Include modal template

   - `kryten_webqueue/templates/catalog/item_detail.html`
     - Add "Edit" button in admin actions
     - Include modal template

---

## API Specification

### `PATCH /admin/catalog/{friendly_token}/edit`

**Auth**: Admin only (rank >= 3)

**Request Body**:
```json
{
  "title": "New Title (1980)",
  "description": "Updated description...",
  "duration_sec": 5400,
  "content_type": "movie",
  "lookup_title": "New Title",
  "lookup_year": "1980",
  "hosted_show": null,
  "tv_show": null,
  "tv_season": null,
  "tv_episode_num": null,
  "sync_to_cms": true,
  "re_enrich": false
}
```

**Response** (success):
```json
{
  "success": true,
  "changes_logged": 3,
  "cms_synced": true,
  "enrich_job_id": null
}
```

**Response** (with re-enrich):
```json
{
  "success": true,
  "changes_logged": 2,
  "cms_synced": false,
  "enrich_job_id": 42
}
```

---

## UI Design

### Modal Layout

```
┌─────────────────────────────────────────────────┐
│  Edit Item                                   ✕  │
├─────────────────────────────────────────────────┤
│                                                 │
│  Core Metadata                                  │
│  ┌───────────────────────────────────────────┐ │
│  │ Title: [New Title (1980)                ]│ │
│  └───────────────────────────────────────────┘ │
│  ┌───────────────────────────────────────────┐ │
│  │ Description:                              │ │
│  │ [Multi-line textarea...                 ]│ │
│  │ [                                       ]│ │
│  └───────────────────────────────────────────┘ │
│  Duration (sec): [5400            ]            │
│                                                 │
│  ──────────────────────────────────────────    │
│                                                 │
│  Advanced (Enrichment State)                    │
│  Content Type: [movie ▼]                        │
│  Lookup Title: [New Title                 ]     │
│  Lookup Year:  [1980    ]                       │
│  Hosted Show:  [        ] (e.g., Svengoolie)    │
│                                                 │
│  TV Episode (if applicable):                    │
│  Show: [             ] Season: [  ] Episode: [ ]│
│                                                 │
│  ──────────────────────────────────────────    │
│                                                 │
│  ☑ Sync title/description to MediaCMS           │
│  ☐ Re-enrich after save (runs classify, title,  │
│     meta, art, tags for this item)              │
│                                                 │
│  [ Save ]  [ Cancel ]                           │
│                                                 │
└─────────────────────────────────────────────────┘
```

---

## Security

- **Authorization**: All edits require rank >= 3 (admin)
- **Input Validation**:
  - Title: 1-500 chars, required
  - Description: max 5000 chars
  - Duration: positive integer or null
  - Content type: enum validation
  - Years: 4-digit integers or null
- **SQL Injection**: Parameterized queries throughout
- **Audit Trail**: Every field change logged with username
- **CSRF**: Session cookie + same-origin credentials

---

## Testing Strategy

### Unit Tests

- `test_edit_item_core_metadata` — edit title/description/duration
- `test_edit_item_enrichment_state` — edit content_type/lookup fields
- `test_edit_item_audit_log` — verify changes are logged
- `test_edit_item_cms_sync` — verify MediaCMS update
- `test_edit_item_re_enrich` — verify job triggered
- `test_edit_item_unauthorized` — 403 for non-admin
- `test_edit_item_validation` — 400 for invalid data

### Manual Testing

- [ ] Edit button visible to admins only
- [ ] Modal opens with pre-filled data
- [ ] Core metadata edits save and reflect
- [ ] Enrichment state edits save and reflect
- [ ] CMS sync checkbox works
- [ ] Re-enrich checkbox triggers job
- [ ] Audit log records all changes
- [ ] Toast notifications work
- [ ] Form validation prevents bad data

---

## Rollout

1. Implement all changes on feature branch
2. Run full test suite (black, ruff, mypy, pytest)
3. Manual QA on local dev instance
4. Update version in `pyproject.toml` → `0.37.0`
5. Update `CHANGELOG.md`
6. Commit, tag `v0.37.0`, push to GitHub
7. Publish to PyPI (automated via GitHub)
8. **DO NOT DEPLOY** — long-running job in progress

---

## Future Enhancements (V2)

- Edit history viewer (show all past edits for an item)
- Bulk edit mode (edit multiple items at once)
- Tag/category editing (requires CMS bulk API improvements)
- Revert to previous version (undo an edit)
- Export edit log as CSV

---

## Open Questions (Resolved)

1. ✅ Include enrichment state in V1? → **Yes**
2. ✅ Include audit logging in V1? → **Yes**
3. ✅ How should re-enrich work? → **Simple: trigger standard job for single item**
4. ✅ Include tag editing? → **No, defer to CMS side**

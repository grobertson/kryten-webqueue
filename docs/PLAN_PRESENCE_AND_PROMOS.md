# Plan — Viewer Presence Refunds & Promo Insertion System

Status: **proposed** (planning only — no code yet)
Target component: `kryten-webqueue`
Author: design session 2026-06-13

This document plans two features requested for the "done for now" milestone:

1. **Presence-based cancel/refund** — when a viewer who paid to queue an item
   leaves the channel or goes AFK, cancel and refund their not-yet-played paid
   items.
2. **Promo insertion system** — maintain curated promo playlists and insert
   short promos between content while a *mutable* playlist is playing, including
   special movie / pay-to-play lead-ins.

The decisions below were confirmed in the planning interview; the
"Resolved decisions" subsections are authoritative.

---

## 0. Current architecture (relevant facts)

- **Poller** ([`queue/poller.py`](../kryten_webqueue/queue/poller.py)) calls
  api-gate every `state_poll_interval_sec` (default 3s), feeding
  `QueueShadow.apply_poll_result(playlist, now_playing)`.
- **QueueShadow** ([`queue/shadow.py`](../kryten_webqueue/queue/shadow.py))
  mirrors the live CyTube playlist in true play order; each item carries
  `uid, position, title, media_type, media_id, duration_sec, is_pay, paid_by,
  tier, z_cost, schedule_id`. It already auto-lifts the event lock when the last
  scheduled item begins (`_maybe_lift_event_lock`).
- **Pay insertion / refund** ([`queue/ordering.py`](../kryten_webqueue/queue/ordering.py)):
  `insert_pay_queue` / `insert_pay_playnext` spend → add → move → record
  `spend_requests`; `refund_item(uid, reason)` looks up the `request_id` for a
  uid and calls `api_gate.queue_refund`.
- **Saved playlists** are `saved_playlists` (+ `saved_playlist_items`) with an
  `is_immutable` flag; immutable playlists are hidden from browse/search and
  excluded from pay-to-play.
- **Mutable vs immutable**: `active_schedule.is_immutable` marks a curated event;
  while true, pay-to-play is locked. "Mutable content" = everything that is **not**
  a running immutable scheduled event.
- **User presence**: `ApiGateClient.get_user(username)` → robot `state.user`,
  returning `{name, rank, online?, meta:{afk, ...}}`, or `{online: False}` when
  the user is not in the channel. There is **no** userlist endpoint in api-gate,
  but per-owner lookups are sufficient (we only check owners of pending paid
  items). No api-gate change is required.
- DB migrations are an ordered list in
  [`catalog/db.py`](../kryten_webqueue/catalog/db.py); latest is **v9**, so new
  migrations begin at **v10**.

---

## 1. Feature 1 — Presence-based cancel/refund

### 1.1 Resolved decisions

| Question | Decision |
| --- | --- |
| Which items | **Only paid (pay-to-play) items that have not started playing.** Free/scheduled items are left alone. |
| Currently-playing item | **Never** cancelled, even if its owner vanished. |
| Trigger | **Leave OR AFK**, both enabled by default. |
| Grace period | **Configurable** (default 60s) before acting; re-check after grace and keep the item if the owner returned / is no longer AFK. |

### 1.2 Config additions (`Config`)

```jsonc
"presence_refund": {
  "enabled": true,
  "on_leave": true,
  "on_afk": false,             // default off until the Robot setAFK fix ships (O1)
  "grace_seconds": 60,
  "check_interval_seconds": 15   // how often to evaluate owners (>= poll interval)
}
```

(Modeled as a nested `PresenceRefundConfig(BaseModel)` like `FetchUrlsConfig`.)

### 1.3 Component: `PresenceRefundMonitor`

New module `kryten_webqueue/queue/presence.py`, started in `app.py` lifespan
(like `StatePoller` / `PlaylistScheduler`). It owns its own loop on
`check_interval_seconds` (decoupled from the 3s state poll to avoid hammering
`get_user`).

Per cycle:

1. Read pending paid items from the shadow: `is_pay = True` and **not** the
   currently-playing uid (`_now_playing_uid`). Collect the distinct set of owner
   usernames (`paid_by`).
2. For each distinct owner, call `api_gate.get_user(username)` once and classify:
   - `online is False` (not in channel) → **gone** (if `on_leave`).
   - online but `meta.afk is True` → **afk** (if `on_afk`).
   - otherwise → **present** → clear any tracked "missing since".
3. Maintain an in-memory `missing_since: dict[username -> (timestamp, reason)]`.
   - First time an owner is seen gone/afk → record `missing_since[user] = now`.
   - When `now - missing_since[user] >= grace_seconds` → act on **all** of that
     owner's pending paid items.
   - If the owner becomes present again before grace elapses → drop the entry
     (the item is kept).
4. **Act** on an item = `refund_item(uid, reason="owner_left" | "owner_afk")`
   then `api_gate.playlist_delete(uid)` to remove it from CyTube, then remove it
   from the shadow. Also remove any **Viewer's Choice lead-in promo** associated
   with that uid (see §2.6).
5. Broadcast a `queue_state` update and (optionally) a chat / WS notice.

### 1.4 Edge cases

- **Owner returns after cancel**: not re-queued (cancellation is final). The
  refund makes them whole; they can re-queue.
- **Multiple items, same owner**: all pending paid items for that owner are
  cancelled together once grace elapses.
- **Item starts playing during grace**: once it is the now-playing item it is
  exempt; only still-pending items are cancelled.
- **`get_user` timeout / error**: treat as *inconclusive* (do not start the
  grace clock); avoids false cancels on a transient robot/NATS hiccup.
- **AFK semantics**: relies on `meta.afk` from the robot's userlist. **This is
  currently stale** — the Robot does not handle CyTube's `setAFK` event, so
  `meta.afk` only reflects join-time state. A Robot fix is a prerequisite for the
  AFK trigger; see Open item **O1** (resolved) for the exact change. Until that
  ships, keep `on_afk` defaulted off; the leave trigger is unaffected.

### 1.5 Tests

- Owner goes offline → after grace, paid item refunded + removed; free item left.
- Owner AFK then returns within grace → item retained, no refund.
- Now-playing item's owner offline → item retained.
- `get_user` raises → no action; next cycle with a real signal acts.
- Two pending items, one owner → both cancelled in one grace window.

---

## 2. Feature 2 — Promo insertion system

### 2.1 Promo types (5)

| # | `promo_type` | Trigger | Selection |
| --- | --- | --- | --- |
| 1 | `channel_identity` | general cadence | per-type config |
| 2 | `event` | general cadence | per-type config |
| 3 | `mod_shoutout` (mod hat-tips) | general cadence | per-type config |
| 4 | `feature_presentation` | a **mutable-playlist** movie (`duration_sec >= 3600`) is the next item | random from pool |
| 5 | `viewers_choice` | a **pay-to-play** item is the next item (**any length**) | random from pool |

Types 1–3 are the "general" promos inserted on a cadence between content.
Types 4–5 are "lead-ins" attached immediately before a specific item.

### 2.2 Resolved decisions

| Question | Decision |
| --- | --- |
| Promo storage | **Reserved `saved_playlists` tagged with a `promo_type`.** Reuse the existing playlist editor; items in the playlist are the promo clips. |
| Promo visibility | Hidden from public browse/search **and** excluded from pay-to-play (same treatment as immutable). |
| Insertion timing | **Just-in-time via the poller** as playback advances (handles looping, pay items, and movies uniformly). Viewer's Choice is inserted deterministically at pay-insertion time — see §2.5. |
| Movie threshold | `duration_sec >= 3600` (>= 60:00). |
| Feature Presentation vs Viewer's Choice | A **paid movie** is "paid" first → gets **Viewer's Choice only**, never Feature Presentation. |
| Stacked promos | A movie due both a cadence general promo **and** an FP/VC lead-in plays: **general promo first, then the FP/VC lead-in immediately before the item.** Order: `[general][FP|VC][content]`. |
| Lead-in cost | Lead-ins are **free** system inserts. If the paid item is later cancelled/refunded, its lead-in promo is removed too. |
| Scope | Promos are inserted **only into mutable content** — never during a running immutable scheduled event (`active_schedule.is_immutable`). |

### 2.3 General promo cadence / selection config

```jsonc
"promos": {
  "enabled": true,
  "movie_threshold_seconds": 3600,
  "general": {
    "every_n_items": 4,          // insert a general promo every N content items
    "every_m_minutes": 20,       // ...or roughly every M minutes, whichever first
    "no_repeat": true            // don't play the same promo clip twice in a row
  },
  "types": {
    "channel_identity": { "enabled": true, "order": "random", "weight": 3 },
    "event":            { "enabled": true, "order": "random", "weight": 2 },
    "mod_shoutout":     { "enabled": true, "order": "sequential", "weight": 1 },
    "feature_presentation": { "enabled": true, "order": "random" },
    "viewers_choice":       { "enabled": true, "order": "random" }
  }
}
```

- `order`: `random` (uniform over the pool) or `sequential` (rotate through the
  pool in stored order, resuming where it left off).
- `weight`: relative frequency among the **general** types when a cadence slot
  fires (the type is chosen by weighted random; the clip within the type is then
  chosen by that type's `order`).
- `no_repeat`: track the last-played clip token per pool and reselect if a draw
  repeats it (skipped for single-item pools).
- Per-type `enabled` lets a type be turned off without deleting its playlist.

### 2.4 Data model changes

**Migration v10** — tag promo playlists:
```sql
ALTER TABLE saved_playlists ADD COLUMN promo_type TEXT;   -- NULL = normal playlist
CREATE INDEX IF NOT EXISTS idx_saved_playlists_promo ON saved_playlists(promo_type);
```
A playlist with a non-NULL `promo_type` is a promo pool. Treated like
`is_immutable` for visibility/pay-exclusion (hidden from browse/search, not
pay-queueable). One designated playlist per type is expected; if several share a
type, their items are unioned into that type's pool.

**Migration v11** — annotate live promo items in the shadow:
```sql
ALTER TABLE queue_shadow ADD COLUMN is_promo BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE queue_shadow ADD COLUMN promo_type TEXT;
ALTER TABLE queue_shadow ADD COLUMN lead_in_for_uid INTEGER;  -- FP/VC: the content uid this promo precedes
```
`QueueShadow` items gain matching keys (`is_promo`, `promo_type`,
`lead_in_for_uid`). `apply_poll_result` preserves these like other local
metadata. Externally-added items default to non-promo.

### 2.5 Component: `PromoDirector`

New module `kryten_webqueue/promos/director.py`, started in `app.py` lifespan.
It is driven by the poll cycle (subscribes to the same reconcile, or runs as a
hook at the end of `apply_poll_result`). It holds in-memory cadence state:

```
content_since_last_general: int
last_general_at: datetime
last_clip_token: dict[promo_type -> str]   # for no_repeat
seq_index: dict[promo_type -> int]         # for sequential order
inserted_general_before_uid: set[int]      # idempotency for general slot
```

**No-op conditions**: `promos.enabled` is false, OR `active_schedule.is_immutable`
is true (running curated event).

Per cycle:

1. **Detect advance**: if now-playing changed since last cycle and the item that
   just finished was **content** (`not is_promo`), increment
   `content_since_last_general` and reset per-slot idempotency markers that have
   now played. Promo items that finished do **not** count as content.
2. **Find the next content item**: first non-promo item after now-playing in play
   order (skip any promos already inserted).
3. **Decide the lead-in for that item** (mutually exclusive):
   - next item `is_pay` → **Viewer's Choice** (any length).
   - else next item is a movie (`duration_sec >= movie_threshold_seconds`) →
     **Feature Presentation**.
   - else → no lead-in.
   Ensure exactly one lead-in promo with `lead_in_for_uid == target_uid` exists
   immediately before the target; if absent and the pool is non-empty/enabled,
   insert one.
4. **Decide a general promo** (cadence): if general enabled AND
   (`content_since_last_general >= every_n_items` OR
   `now - last_general_at >= every_m_minutes`) AND we haven't already inserted a
   general promo for this target (`target_uid not in inserted_general_before_uid`):
   pick a type by weight, a clip by that type's order/no-repeat, and insert it.
   Reset `content_since_last_general = 0`, set `last_general_at = now`, add
   `target_uid` to `inserted_general_before_uid`.
5. **Placement / order**: both promos go **before** the target content item, with
   the general promo before the FP/VC lead-in →
   `[general][FP|VC][target]`. Implemented by `playlist_add(temp=True)` then
   `playlist_move` to the correct slot (reuse the throttled add helper from
   v0.9.13 to avoid 422s). Mark inserted items in the shadow with `is_promo=1`,
   `promo_type`, and (for lead-ins) `lead_in_for_uid`.
6. **Temp items**: promos are added as CyTube **temp** items so they
   auto-remove after playing and never accumulate across loops.

**Viewer's Choice at pay-insertion (determinism)**: because a "play next" paid
item can begin before the next 3s poll, the Viewer's Choice lead-in is inserted
**synchronously inside `insert_pay_queue` / `insert_pay_playnext`** right after
the paid item is positioned — placing the VC promo immediately before the new
paid uid and tagging it `lead_in_for_uid = <paid uid>`. The poller path remains
as a safety net / for playlist-sourced items. This is still "just-in-time", not
load-time. (FP and general promos stay purely poller-driven.)

### 2.6 Removal / refund interactions

- `refund_item` callers (presence monitor §1.3, and the existing
  `move_failed` / schedule-displaced paths) must, after deleting a paid uid,
  delete any shadow item where `lead_in_for_uid == uid` from CyTube and the
  shadow (the orphaned Viewer's Choice lead-in).
- Add a small helper `remove_lead_in_for(uid)` in the promo module and call it
  from the cancel/refund paths.
- When a promo item is observed gone from a poll (it played out as a temp item),
  normal shadow reconciliation removes it; cadence counters are unaffected
  because promos don't count as content.

### 2.7 Admin UI

- **Promo pools page** (or a section on the existing Playlists page): designate a
  saved playlist as a promo pool by choosing its `promo_type` (dropdown:
  none / channel_identity / event / mod_shoutout / feature_presentation /
  viewers_choice). Promo pools are visually flagged and excluded from the public
  catalog like immutable playlists.
- **Promo settings panel**: edit the `promos` config (global enable, cadence
  `every_n_items` / `every_m_minutes`, `no_repeat`, per-type enable / order /
  weight, `movie_threshold_seconds`). If config is file-only today, expose
  read-only display first and make it editable in a follow-up.
- Live queue view: render promo items distinctly (badge by `promo_type`).

### 2.8 Edge cases

- **Empty / disabled pool**: if the chosen type's pool is empty or disabled, skip
  that insertion (no error; for general, try the next weighted type or skip the
  slot).
- **Back-to-back promos**: never insert a general promo before another promo;
  scanning always targets the next **content** item.
- **Movie that is also paid**: paid wins → Viewer's Choice only (no FP).
- **Looping queue**: temp promos disappear after playing; next loop re-inserts by
  cadence, so promos don't pile up.
- **Immutable events**: director is a no-op; curated events play exactly as built.
- **Now-playing is a movie at startup**: no retroactive lead-in (can't precede a
  playing item); applies from the next qualifying upcoming item.
- **Pre-fire lock / fallback**: fallback (mutable) content is eligible for
  promos; the immutable event body is not.

### 2.9 Tests

- Cadence: after N content items, exactly one general promo inserted; counter
  resets; not re-inserted on the next poll for the same slot.
- Minutes cadence fires independently of item count.
- Weighted type selection over many draws approximates configured weights.
- `no_repeat` never selects the same clip twice consecutively (pool size >= 2).
- `sequential` rotates in stored order and resumes.
- Upcoming mutable movie (>=3600s) → FP lead-in immediately before it.
- Upcoming paid item (short) → Viewer's Choice lead-in; paid movie → VC, not FP.
- Stacked order is `[general][FP|VC][content]`.
- Cancelling a paid item removes its VC lead-in.
- Director no-ops during an immutable scheduled event.
- Promo pools hidden from browse/search and rejected by pay-to-play.

---

## 3. Cross-cutting

- **Startup wiring** (`app.py` lifespan): construct and start
  `PresenceRefundMonitor` and `PromoDirector` after the shadow/poller/scheduler,
  passing `api_gate`, `db`, `shadow`, `ws_manager`, and the new config blocks;
  stop them on shutdown.
- **Reuse the throttled add helper** (`playlists/bulk_add.py`, v0.9.13) for all
  promo inserts to avoid transient CyTube `queueFail`/422s.
- **Config example**: add `presence_refund` and `promos` blocks to
  `config.example.json` with the defaults above.
- **CHANGELOG + version**: ship as a minor bump (e.g. `0.10.0`) given the new
  subsystems; update `CHANGELOG.md`.
- **Docs**: link this plan from `docs/IMPLEMENTATION_SPEC.md` once implemented.

## 4. Suggested implementation order

1. Migrations v10/v11 + shadow field plumbing (no behavior change).
2. `PresenceRefundConfig` + `PresenceRefundMonitor` + tests (self-contained).
3. Promo data model: `promo_type` on playlists, visibility/pay exclusion, admin
   designation UI.
4. `PromoConfig` + `PromoDirector` general cadence (types 1–3) + tests.
5. Feature Presentation lead-in (type 4) + tests.
6. Viewer's Choice (type 5): synchronous pay-insertion hook + cancel cleanup +
   tests.
7. Admin promo settings panel + live-queue badges.
8. Config example, CHANGELOG, version bump, release.

## 5. Open items / assumptions to confirm during build

- **O1 — AFK source**: confirm `get_user().meta.afk` is populated by the robot's
  userlist in practice. If not, options: (a) add a userlist/AFK passthrough in
  api-gate, or (b) ship with `on_afk` defaulting off until verified.

  **RESOLVED (2026-06-13, code-traced):**
  - **Leave detection works as planned.** CyTube `userLeave` →
    `state_manager.remove_user()` → user drops from the userlist →
    `get_user()` returns `None` → api-gate `state/user` returns
    `{"online": False}`. No change needed for the leave path.
  - **AFK detection is currently broken — needs a Robot fix.** `get_user()`
    returns the raw stored CyTube user object and the Robot *expects* a
    `meta.afk` field (it already reads it for user-counting in
    [`state_manager.py`](../../Kryten-Robot/kryten/state_manager.py) ~L119).
    **However**, the Robot only refreshes user `meta` on the `userlist` and
    `addUser` events ([`__main__.py`](../../Kryten-Robot/kryten/__main__.py)
    ~L341–L348). CyTube's **`setAFK`** event (`{name, afk}`) is **not** in the
    registered `state_events` list and is never dispatched, so `meta.afk` only
    reflects the user's AFK state *at join time* and goes stale afterward.
  - **Required Robot change (prerequisite for `on_afk`)**: handle the `setAFK`
    event. Add `"setAFK"` to the `state_events` list and a dispatch branch that
    merges `afk` into the stored user's `meta`, e.g.:
    ```python
    elif event_name == "setAFK":
        name = payload.get("name")
        if name is not None:
            existing = state_manager.get_user(name) or {"name": name}
            meta = {**existing.get("meta", {}), "afk": bool(payload.get("afk"))}
            await state_manager.update_user({**existing, "name": name, "meta": meta})
    ```
    (Confirm CyTube's `setAFK` payload shape against the live socket;
    historically `{name, afk}`.) Ship this Robot fix first, or ship webqueue with
    `on_afk` defaulting **off** and flip it on once the Robot change is deployed.
  - No api-gate change is needed for either path; `state/user` already passes the
    stored `meta` through.

  **DONE (Robot v1.10.0, 2026-06-13):** the Robot now handles `setAFK` via
  `StateManager.set_user_afk()` (merges into `meta` in place, preserves other
  fields, skips redundant KV writes) and registers the event in `state_events`.
  Covered by `tests/test_state_manager_afk.py`. Once Robot v1.10.0 is deployed,
  `on_afk` can be safely defaulted **on** in webqueue's presence-refund config.
- **O2 — Config editability**: is runtime editing of `promos` config in-scope, or
  is file-config + restart acceptable for the first cut? (Plan assumes file
  config first, editable panel as a follow-up.)
- **O3 — Single vs multiple pools per type**: plan supports multiple playlists of
  the same `promo_type` (unioned). Confirm one-per-type is acceptable in the UI.
- **O4 — Notifications**: should a cancelled-on-disappear item post a chat/PM
  notice, or refund silently? (Plan: silent refund + WS state update; easy to add
  a notice.)

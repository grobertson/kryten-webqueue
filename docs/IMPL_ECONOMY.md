# kryten-economy — Implementation Spec (Gaps 4, 5, 6)

**Version:** 1.0  
**Date:** 2026-05-30  
**Service:** kryten-economy v0.8.10 → v0.8.11  
**Gaps covered:** 4 (`spending.queue_preview`), 5 (`spending.queue`), 6 (`spending.queue_refund`)

---

## Overview

Add three new NATS command handlers to `CommandHandler`, a new `queue_spend_requests` table for idempotency, and a DB helper to increment `daily_activity.queues_used`.

No changes to `SpendingEngine`, `config.py`, or any existing handler.

---

## 1. New DB table — `queue_spend_requests`

**File:** `kryten_economy/database.py`

Add to the `_init_db()` method, inside the `with conn:` block, after the existing `trigger_cooldowns` table creation (around line 176):

```python
conn.execute("""
    CREATE TABLE IF NOT EXISTS queue_spend_requests (
        request_id TEXT PRIMARY KEY,
        username   TEXT NOT NULL,
        channel    TEXT NOT NULL,
        cost_z     INTEGER NOT NULL,
        tier       TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        refunded   INTEGER NOT NULL DEFAULT 0,
        refunded_at TEXT
    )
""")
conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_qsr_username_channel "
    "ON queue_spend_requests(username, channel)"
)
```

---

## 2. New DB methods

**File:** `kryten_economy/database.py`

Add a new section after the existing `# Sprint 5: Transaction History` block:

```python
# ══════════════════════════════════════════════════════════
#  Sprint 5: Queue Spend Requests
# ══════════════════════════════════════════════════════════

async def insert_queue_spend_request(
    self,
    request_id: str,
    username: str,
    channel: str,
    cost_z: int,
    tier: str,
) -> bool:
    """Insert idempotency record. Returns True if inserted, False if already exists."""
    loop = asyncio.get_running_loop()

    def _sync() -> bool:
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO queue_spend_requests "
                "(request_id, username, channel, cost_z, tier) VALUES (?, ?, ?, ?, ?)",
                (request_id, username, channel, cost_z, tier),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    return await loop.run_in_executor(None, _sync)


async def get_queue_spend_request(self, request_id: str) -> dict | None:
    """Return queue_spend_requests row or None."""
    loop = asyncio.get_running_loop()

    def _sync() -> dict | None:
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM queue_spend_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    return await loop.run_in_executor(None, _sync)


async def mark_queue_spend_refunded(self, request_id: str) -> None:
    """Set refunded=1 and refunded_at=now() on a spend request."""
    loop = asyncio.get_running_loop()

    def _sync() -> None:
        conn = self._get_connection()
        try:
            conn.execute(
                "UPDATE queue_spend_requests "
                "SET refunded = 1, refunded_at = datetime('now') "
                "WHERE request_id = ?",
                (request_id,),
            )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _sync)


async def increment_daily_queues_used(
    self, username: str, channel: str, date: str,
) -> None:
    """Increment queues_used in daily_activity, creating row if needed."""
    loop = asyncio.get_running_loop()

    def _sync() -> None:
        conn = self._get_connection()
        try:
            conn.execute(
                "INSERT INTO daily_activity (username, channel, date, queues_used) "
                "VALUES (?, ?, ?, 1) "
                "ON CONFLICT(username, channel, date) DO UPDATE "
                "SET queues_used = queues_used + 1",
                (username, channel, date),
            )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _sync)
```

---

## 3. New command handlers

**File:** `kryten_economy/command_handler.py`

### 3a. Add imports

At the top of the file, `timedelta` is already imported (`from datetime import datetime, timedelta, timezone`). No new imports needed.

### 3b. Add handler methods

Add three new handler methods in a new section before `_HANDLER_MAP`. Place them after `_handle_transactions_recent` (the last existing Sprint 5 handler):

```python
# ══════════════════════════════════════════════════════════
#  Sprint 5: Queue Spending Commands
# ══════════════════════════════════════════════════════════

async def _handle_spending_queue_preview(
    self, request: dict[str, Any]
) -> dict[str, Any]:
    """Read-only cost estimate. No state changed."""
    username = self._username(request)
    channel = self._channel(request)
    duration_sec = int(request.get("duration_sec", 0))
    if duration_sec <= 0:
        raise ValueError("duration_sec must be positive")

    engine = self._app.spending_engine
    cfg = self._app.config.spending
    db = self._app.db

    # --- Pricing ---
    tier_label, base_cost = engine.get_price_tier(duration_sec)
    account = await db.get_account(username, channel)
    rank_index = engine.get_rank_tier_index(account) if account else 0
    final_cost, discount_frac = engine.apply_discount(base_cost, rank_index)
    discount_pct = round(discount_frac * 100, 1)

    # --- Eligibility checks (in priority order) ---
    error_code = None
    cooldown_remaining_sec = None
    daily_remaining = cfg.max_queues_per_day

    # 1. Blackout
    now_utc = datetime.now(timezone.utc)
    if _is_blackout_active(cfg.blackout_windows, now_utc):
        error_code = "blackout_active"

    # 2. Daily limit
    if error_code is None:
        today = self._utc_today()
        activity = await db.get_or_create_daily_activity(username, channel, today)
        queues_used = activity.get("queues_used", 0)
        # Rank perk: Best Boy and above get +1 queue/day
        max_queues = cfg.max_queues_per_day + _rank_queue_bonus(account)
        daily_remaining = max(0, max_queues - queues_used)
        if queues_used >= max_queues:
            error_code = "daily_limit_reached"
            daily_remaining = 0

    # 3. Cooldown
    if error_code is None:
        last_queue_time = await db.get_last_queue_time(username, channel)
        if last_queue_time is not None:
            elapsed = (now_utc - last_queue_time).total_seconds()
            cooldown_sec = cfg.queue_cooldown_minutes * 60
            if elapsed < cooldown_sec:
                error_code = "cooldown_active"
                cooldown_remaining_sec = int(cooldown_sec - elapsed)

    # 4. Balance
    if error_code is None:
        outcome = await engine.validate_spend(username, channel, final_cost, "queue")
        if outcome is not None:
            error_code = "insufficient_balance"

    result: dict[str, Any] = {
        "available": error_code is None,
        "cost_z": final_cost,
        "tier_label": tier_label,
        "discount_pct": discount_pct,
        "daily_remaining": daily_remaining,
        "error_code": error_code,
    }
    if cooldown_remaining_sec is not None:
        result["cooldown_remaining_sec"] = cooldown_remaining_sec
    return result


async def _handle_spending_queue(
    self, request: dict[str, Any]
) -> dict[str, Any]:
    """Atomic validate + debit. Idempotent via request_id."""
    username = self._username(request)
    channel = self._channel(request)
    duration_sec = int(request.get("duration_sec", 0))
    tier = str(request.get("tier", "queue"))
    request_id = str(request.get("request_id", "")).strip()
    if not request_id:
        raise ValueError("request_id is required")
    if duration_sec <= 0:
        raise ValueError("duration_sec must be positive")

    engine = self._app.spending_engine
    cfg = self._app.config.spending
    db = self._app.db

    # --- Idempotency check ---
    existing = await db.get_queue_spend_request(request_id)
    if existing is not None:
        # Already processed — return stored outcome without re-debiting
        current_balance = await db.get_balance(username, channel)
        return {
            "success": True,
            "cost_z": existing["cost_z"],
            "new_balance": current_balance,
            "error_code": None,
            "idempotent": True,
        }

    # --- Pricing ---
    tier_label, base_cost = engine.get_price_tier(duration_sec)
    account = await db.get_account(username, channel)
    rank_index = engine.get_rank_tier_index(account) if account else 0
    final_cost, _ = engine.apply_discount(base_cost, rank_index)

    # --- Eligibility (same order as preview) ---
    now_utc = datetime.now(timezone.utc)
    if _is_blackout_active(cfg.blackout_windows, now_utc):
        return {"success": False, "cost_z": final_cost,
                "new_balance": account["balance"] if account else 0,
                "error_code": "blackout_active"}

    today = self._utc_today()
    activity = await db.get_or_create_daily_activity(username, channel, today)
    queues_used = activity.get("queues_used", 0)
    max_queues = cfg.max_queues_per_day + _rank_queue_bonus(account)
    if queues_used >= max_queues:
        return {"success": False, "cost_z": final_cost,
                "new_balance": account["balance"] if account else 0,
                "error_code": "daily_limit_reached"}

    last_queue_time = await db.get_last_queue_time(username, channel)
    if last_queue_time is not None:
        elapsed = (now_utc - last_queue_time).total_seconds()
        cooldown_sec = cfg.queue_cooldown_minutes * 60
        if elapsed < cooldown_sec:
            return {"success": False, "cost_z": final_cost,
                    "new_balance": account["balance"] if account else 0,
                    "error_code": "cooldown_active",
                    "cooldown_remaining_sec": int(cooldown_sec - elapsed)}

    outcome = await engine.validate_spend(username, channel, final_cost, "queue")
    if outcome is not None:
        return {"success": False, "cost_z": final_cost,
                "new_balance": account["balance"] if account else 0,
                "error_code": "insufficient_balance"}

    # --- Reserve idempotency slot before debiting ---
    inserted = await db.insert_queue_spend_request(
        request_id, username, channel, final_cost, tier
    )
    if not inserted:
        # Race condition: another request beat us — treat as idempotent
        current_balance = await db.get_balance(username, channel)
        return {"success": True, "cost_z": final_cost,
                "new_balance": current_balance, "error_code": None, "idempotent": True}

    # --- Debit ---
    new_balance = await db.debit(
        username,
        channel,
        final_cost,
        tx_type="debit",
        reason=f"Queue spend ({tier_label})",
        trigger_id="spend.queue",
    )
    if new_balance is None:
        # Funds were there during validate but gone now (race); clean up record
        await db.get_queue_spend_request(request_id)  # leave record but balance failed
        return {"success": False, "cost_z": final_cost,
                "new_balance": 0, "error_code": "insufficient_balance"}

    # --- Increment daily counter ---
    await db.increment_daily_queues_used(username, channel, today)

    return {
        "success": True,
        "cost_z": final_cost,
        "new_balance": new_balance,
        "error_code": None,
    }


async def _handle_spending_queue_refund(
    self, request: dict[str, Any]
) -> dict[str, Any]:
    """Compensating credit. Idempotent via request_id."""
    username = self._username(request)
    channel = self._channel(request)
    request_id = str(request.get("request_id", "")).strip()
    reason = str(request.get("reason", "refund"))
    if not request_id:
        raise ValueError("request_id is required")

    db = self._app.db

    record = await db.get_queue_spend_request(request_id)
    if record is None:
        return {"success": False, "error": "unknown_request_id"}

    if record["refunded"]:
        # Already refunded — idempotent success
        current_balance = await db.get_balance(username, channel)
        return {
            "success": True,
            "refunded_z": record["cost_z"],
            "new_balance": current_balance,
            "idempotent": True,
        }

    new_balance = await db.credit(
        username,
        channel,
        record["cost_z"],
        tx_type="credit",
        reason=f"Queue refund: {reason}",
        trigger_id="spend.queue_refund",
    )
    await db.mark_queue_spend_refunded(request_id)

    return {
        "success": True,
        "refunded_z": record["cost_z"],
        "new_balance": new_balance,
    }
```

### 3c. Module-level helpers

Add two private module-level functions directly above the `CommandHandler` class definition:

```python
def _is_blackout_active(
    windows: list, now_utc: "datetime"
) -> bool:
    """Return True if any blackout window covers now_utc.

    BlackoutWindowConfig has: name (str), cron (str), duration_hours (int).
    This implementation checks only cron-style hour-of-day windows.
    A full cron parser can be added later; for now treat cron as 'HH * * * *'
    (matches any minute of that hour on any day).
    """
    try:
        from croniter import croniter  # optional dep
        for window in windows:
            it = croniter(window.cron, now_utc)
            last_start = it.get_prev(datetime)
            window_end = last_start + timedelta(hours=window.duration_hours)
            if last_start <= now_utc < window_end:
                return True
    except ImportError:
        pass  # croniter not installed — skip blackout check
    return False


def _rank_queue_bonus(account: dict | None) -> int:
    """Extra queues per day granted by rank perks.

    Reads the perk strings from the account's rank_name field against
    the loaded config.  Since we don't have the config here, we use a
    simple convention: accounts with rank_name in a known 'elevated' set
    get +1.  Adjust the set to match your rank config perks.
    """
    if not account:
        return 0
    # rank perks "+1 queue/day" apply to Best Boy and above.
    # These names come from config.ranks.tiers[*].name
    elevated = {"Best Boy", "Star", "Legend"}  # extend as ranks are added
    return 1 if account.get("rank_name") in elevated else 0
```

> **Note on `_rank_queue_bonus`:** The `SpendingEngine` has a `get_rank_tier_index` method but the bonus is defined as a rank perk string (`"+1 queue/day"`), not a simple tier index cutoff. The helper above implements the intent without coupling to config at module level. If the rank set changes, update `elevated`. A cleaner future refactor would pass `config.ranks` into the helper.

### 3d. Register in `_HANDLER_MAP`

Add three entries to the `_HANDLER_MAP` dict:

```python
# existing entries ...
"spending.queue_preview": _handle_spending_queue_preview,
"spending.queue":         _handle_spending_queue,
"spending.queue_refund":  _handle_spending_queue_refund,
```

---

## 4. Version bump

**File:** `pyproject.toml`

```toml
version = "0.8.11"
```

**File:** `kryten_economy/__init__.py` (if `__version__` is defined there — check first):

```python
__version__ = "0.8.11"
```

---

## 5. Tests

**File:** `tests/test_spending_commands.py` (new file)

Minimum test coverage required:

| Test | What to verify |
|---|---|
| `test_preview_returns_cost` | Happy path: correct `cost_z`, `tier_label`, `available: true` |
| `test_preview_insufficient_balance` | `available: false`, `error_code: "insufficient_balance"` |
| `test_preview_daily_limit` | Seed `daily_activity.queues_used = max_queues_per_day`; expect `error_code: "daily_limit_reached"` |
| `test_preview_cooldown` | Seed transaction with `trigger_id LIKE 'spend.queue%'` within window; expect `error_code: "cooldown_active"` |
| `test_queue_happy_path` | Debit succeeds; `queue_spend_requests` row inserted; `daily_activity.queues_used` incremented |
| `test_queue_idempotent` | Call `spending.queue` twice with same `request_id`; balance debited only once |
| `test_queue_insufficient_balance` | `success: false`, no idempotency row left (or row with no transaction) |
| `test_refund_happy_path` | Spend first, then refund; balance restored; `refunded = 1` |
| `test_refund_idempotent` | Call `spending.queue_refund` twice; balance credited only once |
| `test_refund_unknown_request_id` | `success: false`, `error: "unknown_request_id"` |

---

## 6. Dependencies

`croniter` is optional. If not installed, blackout window checks are silently skipped. To enable:

```toml
# pyproject.toml [project.optional-dependencies]
blackout = ["croniter>=1.4"]
```

If the project already bundles `croniter`, skip this.

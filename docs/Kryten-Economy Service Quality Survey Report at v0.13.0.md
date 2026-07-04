## Kryten-Economy Service Survey Report

### 1. What It Does

**kryten-economy** (v0.13.0) is a standalone NATS-based microservice providing a channel engagement currency system for CyTube. It depends on `kryten-py` (NATS client) and operates independently — **no other workspace imports it as a Python dependency**.

| Workspace | References kryten-economy? |
|-----------|---------------------------|
| kryten-api-gate | **No** Python imports |
| kryten-py | **No** references |
| kryten-cli | **No** references |
| Kryten-Robot | **No** references |
| kryten-webqueue | HTTP reference via config (`web_queue_url`), no Python import |

It communicates via NATS pub-sub with Kryten-Robot (for user presence events) and kryten-api-gate (for queue spending). The architecture is clean and well-isolated.

### 2. File Size Analysis (Maintenance Concern)

The service consists of 35 source files totaling ~727 KB. Two files dominate:

| File | Size | % of Codebase | Issue |
|------|------|---------------|-------|
| `database.py` | **146 KB** | 20% | Single SQLite data-access layer with all tables, migrations, account ops, gambling, tips, schedules, etc. |
| `pm_handler.py` | **141 KB** | 19% | Massive PM command dispatcher handling all user-facing commands inline |
| `command_handler.py` | **62 KB** | 8.5% | NATS request-reply handler |
| `gambling_engine.py` | **39 KB** | 5.4% | Slots, flips, challenges, heists |
| `race_engine.py` | **42 KB** | 5.8% | Race betting engine |
| `config.py` | **45 KB** | 6.2% | Pydantic models for all 9 sprints |
| **Top 2 files** | **287 KB** | **39%** | The two largest files account for nearly 40% of the codebase |

### 3. Recommendations: Remove / Clean Up

#### A. `database.py` (146 KB) — **SPLIT INTO SUB-MODULES**
This is the single biggest maintainability issue. The file contains:
- `_create_tables()` with 30+ table definitions and migration logic (~50 lines of inline ALTER TABLE migrations)
- 60+ public async methods covering accounts, transactions, daily activity, streaks, bridges, hourly milestones, gambling stats, challenges, race results, trivia stats, blackjack stats, tips, approvals, vanity items, achievements, bounties, snapshots, bans, queue-spend requests, service metrics, and more

**Recommendation**: Split into:
- `database/__init__.py` — connection helpers, initialization, migrations
- `database/accounts.py` — get/create account, balance, credit/debit/refund
- `database/gambling.py` — spins, flips, challenges, race results, trivia, blackjack
- `database/transactions.py` — transaction history, daily activity
- `database/playlists.py` — queue spend requests
- `database/social.py` — tips, streaks, hourly milestones, vanity items
- `database/admin.py` — snapshots, bans, service metrics, bounties

#### B. `pm_handler.py` (141 KB) — **SPLIT INTO SUB-MODULES**
This is a single-file command dispatcher handling every user-facing command (`balance`, `tip`, `queue`, `search`, `spin`, `flip`, `heist`, `race`, `trivia`, `blackjack`, `achievements`, `rank`, `bounty`, `shop`, `fortune`, `shoutout`, `greeting`, `color`, `gif`, `help`, plus admin commands for bans, snapshots, config reload, etc.). It's 3,231 lines.

**Recommendation**: Split into:
- `pm_handler/__init__.py` — dispatcher, rate limiter, PM worker
- `pm_handler/gambling.py` — spin, flip, challenge, heist commands
- `pm_handler/games.py` — race, trivia, blackjack commands
- `pm_handler/shop.py` — vanity purchases (color, greeting, gif, shoutout, fortune)
- `pm_handler/admin.py` — admin commands (ban, snapshot, config reload, etc.)
- `pm_handler/queue.py` — queue, search, playnext commands

#### C. `config.py` (45 KB) — **CONSIDER REDUCING VERBOSITY**
The config has 50+ Pydantic models organized by sprint labels. While well-structured, many sub-configs reference LLM back-ends (`HeistLLMConfig`, `RaceLLMConfig`) that default to Ollama endpoints and may not be actively used depending on the `mode` setting (`static`/`llm`/`hybrid`). Similarly, `RaceOddsProfileConfig` with three complete 8-racer profiles defaults adds ~100 lines of inline defaults.

**Recommendation**: Move large default values (race odds profiles, payout tables) into separate data files or constants, keeping the config model lean.

#### D. Unused/Dead Config Fields
- `HeistConfig.enabled` defaults to `False` — heists are disabled by default and the feature depends on an LLM narrative mode. If not deployed, the entire heist subsystem (`heist_narrator.py`, `heist_narratives.py`) is dead code.
- `RaceLLMConfig` defaults to `llama3` on `localhost:11434` — the commentary `mode` defaults to `static`, so LLM-based race commentary is opt-in. The LLM config is only live if operators configure it.

#### E. Narrative Modules — CONDITIONAL DEAD CODE
- `heist_narratives.py` (22 KB) — built-in narrative templates, only used when `HeistConfig.narrative.mode` is `static` (the default, since `llm` requires an LLM endpoint)
- `heist_narrator.py` (13 KB) — bridges narratives to LLM or static library
- `race_narratives.py` (8 KB) — built-in race commentary templates
- `race_narrator.py` (17 KB) — bridges race commentary to LLM or static library

These are well-factored with a clean static/LLM/hybrid architecture. They're not *dead* code, but they account for 60 KB of narrative content that may be partially unused depending on deployment config. Consider documenting the dependency chain clearly.

### 4. Architectural Observations

#### A. Database Connection Pattern
Every public method opens a new SQLite connection via `run_in_executor`, and each one creates a WAL-mode connection with a 30-second busy timeout. While this works (SQLite WAL supports concurrent readers), it creates overhead for simple operations like `get_balance`. A connection pool (even a simple one with 1-2 cached connections) would improve latency for high-frequency operations.

#### B. Test Suite Health

The test suite is **comprehensive**: 90+ test files with a well-designed `conftest.py` providing shared fixtures for all major engines. Key observations:
- `conftest.py` (510 lines) is well-organized by sprint, with good use of `AsyncMock` and `MockKrytenClient`
- Tests import virtually everything from production code — no orphaned test files detected
- The `MockKrytenClient` class in conftest is excellent for integration tests
- No obviously skipped or always-failing tests found in the checked files

#### C. Cross-Workspace Isolation

Economy is architecturally pure: zero Python imports from other workspaces, communicates only via NATS. This is a strength — no coupling issues to worry about.

#### D. EventAnnouncer + GreetingHandler (Sprint 9)
These are clean, well-designed modules that centralize chat announcements through a deduplication/rate-limiting layer. They're properly wired in `EconomyApp.start()` and appear to be actively used.

#### E. MulitplierEngine + ScheduledEventManager
These form a clean multiplier pipeline. The `MultiplierEngine` correctly uses multiplicative composition and handles scheduled/adhoc/holiday/population multipliers. No issues found.

### 5. Summary of Actionable Recommendations

| Priority | Recommendation | Effort | Impact |
|----------|---------------|--------|--------|
| **High** | Split `database.py` (146 KB) into sub-modules | 1-2 days | Major — reduces cognitive load, enables focused testing |
| **High** | Split `pm_handler.py` (141 KB) into sub-modules | 1-2 days | Major — the file is too large to maintain effectively |
| **Medium** | Document which narrative/game features are active by default vs. opt-in | 1 hour | Prevents confusion about what's dead vs. dormant |
| **Medium** | Consider connection pooling for `database.py` | 0.5 day | Minor latency improvement for high-frequency ops |
| **Low** | Trim default race odds profiles from config.py into data file | 0.5 day | Minor — reduces config verbosity |
| **Low** | Default heist to enabled or document why it's off | 1 hour | Clarifies intent |

### 6. Overall Assessment

The codebase is **well-architected, well-documented, and actively maintained**. It follows a consistent pattern (sprint-based feature additions), has comprehensive test coverage (~90+ test files), and uses modern async Python patterns throughout. The major issues are structural: two files (`database.py` and `pm_handler.py`) together account for 40% of the entire codebase, making them difficult to navigate and maintain. Splitting these into sub-modules would significantly improve developer experience. No actual dead code requiring removal was found — the narrative/game modules are conditionally used based on configuration, which is an intentional architectural choice.
# SPEC — Sortie 2: Multi-Database Connection Layer

**Sprint**: `sqlite-domain-separation`  
**PRD**: [PRD-sqlite-domain-separation.md](PRD-sqlite-domain-separation.md)  
**Depends on**: Sortie 1  
**Estimated Effort**: 4 hours  

---

## 1. Overview

Build a multi-database connection management architecture where the top-level `Database` facade coordinates 4 specialized domain sub-connections (`catalog`, `queue`, `jobs`, `users`). Each connection maintains one dedicated `aiosqlite` dispatcher, independent WAL configuration, timeout settings, and domain-scoped observability.

---

## 2. Architecture & Design

### 2.1 Domain Database Instances

```python
class _DomainDB:
    def __init__(self, db_path: str, migrations: list[str]):
        self._db_path = db_path
        self._migrations = migrations
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=10000")
        await self._db.execute("PRAGMA wal_autocheckpoint=100")
        await self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")

    async def close(self):
        if self._db:
            await self._db.close()
```

The `busy_timeout` is a bounded wait budget, not a guarantee against same-domain contention.
Write paths must keep transactions short and bounded, and log retry count, operation latency, and
the affected domain. The design deliberately does not add a per-domain connection pool in SQLite.

### 2.2 Unified Facade

```python
class Database:
    def __init__(self, config: Config):
        self.catalog = _CatalogDB(config.get_catalog_path())
        self.queue = _QueueDB(config.get_queue_path())
        self.jobs = _JobsDB(config.get_jobs_path())
        self.users = _UsersDB(config.get_users_path())

    async def connect(self):
        await asyncio.gather(
            self.catalog.connect(),
            self.queue.connect(),
            self.jobs.connect(),
            self.users.connect(),
        )

    async def run_migrations(self):
        await asyncio.gather(
            self.catalog.run_migrations(),
            self.queue.run_migrations(),
            self.jobs.run_migrations(),
            self.users.run_migrations(),
        )

    async def close(self):
        await asyncio.gather(
            self.catalog.close(),
            self.queue.close(),
            self.jobs.close(),
            self.users.close(),
        )
```

---

## 3. Implementation Plan

1. **Create** `kryten_webqueue/catalog/db/_base_domain.py` implementing `_DomainDB`.
2. **Implement** domain wrappers:
   - `_catalog_db.py` (mixes in `_CatalogMixin`, `_PeopleMixin`, `_EnrichmentMixin`, `_MOTDMixin`)
   - `_queue_db.py` (mixes in `_QueueMixin`, `_PlaylistsMixin`, `_BlackoutMixin`)
   - `_jobs_db.py` (mixes in `_FetchQueueMixin`, job runs & logs)
   - `_users_db.py` (mixes in `_WatchlistMixin`, `_DevicesMixin`, `_FeedbackMixin`)
3. **Refactor** `Database` in `kryten_webqueue/catalog/db/__init__.py` to delegate public methods to respective domains.
4. **Update** `app.py` lifespan to initialize all 4 database connections concurrently.

Each domain applies only its new baseline/forward migration history. Legacy monolith
`_migrations` rows and historic migration side effects are never reused.

---

## 4. Acceptance Criteria

- [ ] All 4 SQLite databases initialize in parallel on app startup.
- [ ] Each database executes its domain migrations independently.
- [ ] Public methods on `Database` retain identical signatures and return shapes.
- [ ] Existing route calls (`request.app.state.db.*`) work without modification.
- [ ] Same-domain writes expose retry and latency metrics; the implementation makes no claim that SQLite writers within one file execute concurrently.

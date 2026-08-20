# SPEC — Local TMDB Index & Identity-First Enrichment (Sorties)

**PRD**: [`PRD_TMDB_LOCAL_INDEX.md`](PRD_TMDB_LOCAL_INDEX.md)
**Sprint goal**: Build a local TMDB index from the daily dumps, resolve every catalog item to a
stable `tmdb_id` + IMDb `tt#`, and make art/meta key off that identity. Add a coverage report.

Sorties are ordered by dependency. Each is independently testable. High-stakes surfaces flagged
per AGENTS.md: enrichment classify/identity logic (needs `force` to re-apply), a new job with
schema-validated params, new config keys, and `catalog.imdb_tt` writes (v23 unique index).

---

## Sortie 1 — Local TMDB index: schema, builder, refresh job

**Objective**: Turn the JSONL dumps into a queryable local SQLite index, rebuilt by a job.

**Scope**
- New module `kryten_webqueue/catalog/tmdb_index/` with:
  - `_schema.py` — index DB schema (see PRD §5.1). This DB is **separate** from `webqueue.db`;
    it is fully rebuilt each refresh, so it uses a plain `CREATE` schema, **not** the
    `MIGRATIONS` chain in `catalog/db/_connection.py`.
  - `builder.py` — `build_index(dump_dir, index_path, kinds) -> BuildStats`. Stream each JSONL
    file line-by-line (`for line in f`), `json.loads` per line, insert in batches
    (`executemany`, ~5k rows) into a **temp** DB path, populate `norm_title` via the shared
    `_norm`, then atomically replace `index_path`. Blocking/sync — callers run it via
    `asyncio.to_thread`.
  - `__init__.py` — exports `build_index`, `TMDBLocalIndex` (Sortie 2).
- New job `tmdb_index_refresh` in `kryten_webqueue/jobs/tasks.py` + registration wherever jobs
  are registered. Param schema (validated by `jobs/manager.validate_params`):
  - `source`: enum `["local"]` (default `local`; `download` reserved for a later sortie).
  - `dump_dir`: string, default from config `tmdb_index_source_dir`. **Validate** it resolves
    under the configured directory — reject arbitrary paths.
  - `kinds`: enum, default `"movies,tv"`; allowed exactly:
    `"movies,tv"`, `"movies"`, `"tv"`, `"all"`.
  - Job runs `build_index` off-loop, reports progress (`ctx.progress`) per file/batch, and
    records counts to run history.
- APScheduler daily cron entry via existing `jobs/job_scheduler.py` (disabled by default; admin
  enables). Do **not** auto-run on startup.
- Config: add `tmdb_index_path` and `tmdb_index_source_dir` to `config.example.json` and the
  Pydantic config model.

**Files**
- New: `catalog/tmdb_index/{__init__,_schema,builder}.py`
- Edit: `jobs/tasks.py`, job registry, `jobs/job_scheduler.py`, config model, `config.example.json`

**Testing**
- Unit: `build_index` over a tiny fixture dump dir (a handful of JSONL lines per kind) →
  asserts row counts, `norm_title` values, FTS searchability, `index_meta` populated.
- Unit: atomic swap leaves a valid index if a prior one existed; partial build in temp doesn't
  corrupt the live file.
- Job: param validation rejects a bad `kinds`, rejects a `dump_dir` outside the allowed root.

**Acceptance criteria**
- [ ] `build_index` builds movies+tv (and optionally people/keywords/companies/networks) from a
      dump dir into a standalone SQLite file, streaming (bounded memory).
- [ ] `tmdb_index_refresh` job registered, single-flight, schema-validated, off-loop, with
      progress + run-history.
- [ ] Config keys added and documented; `config.example.json` in sync.
- [ ] Tests green; `black`/`ruff`/`mypy` clean.

---

## Sortie 2 — Offline resolver `TMDBLocalIndex.resolve` (ideas #1 + #2)

**Objective**: Resolve a title to a `tmdb_id` with no network call, typo/fuzzy tolerant.

**Scope**
- `catalog/tmdb_index/index.py`: `class TMDBLocalIndex` opening `tmdb_index_path` read-only
  (`aiosqlite`, `mode=ro`), with:
  - `async resolve(title: str, year: str | None = None, kind: str = "movie", *, original_title: str | None = None) -> ResolveResult | None`
    implementing PRD §5.2 (exact norm → FTS5 MATCH → rank by exact / `difflib` ratio /
    `-popularity`). Matches against **both** the normalized `original_title` column and the
    normalized query; when `original_title` is provided it is tried first and ranked above
    English-only matches (highest-yield offline signal). Reuse `_norm` and `_titles_similar`
    from `enrichment/providers.py` (extract to a shared spot if a circular import forces it —
    otherwise import directly).
  - `ResolveResult` dataclass: `tmdb_id: int`, `matched_title: str`, `popularity: float`,
    `confidence: Literal["exact","high","low"]`, `matched_on: Literal["original_title","title"]`.
  - `async close()`.
- **`extract_imdb_tt(*texts: str) -> str | None`** (in `catalog/tmdb_index/_ttscrape.py` or
  `enrichment/normalise.py`): regex-scan for `imdb\.com/title/(tt\d{7,8})` or a delimited bare
  `\btt\d{7,8}\b`; return the first valid id or `None`, never raise. Pure/sync, trivially
  testable, used by `identify` in Sortie 3.
- Handle a **missing/empty index** gracefully: `resolve` returns `None` and logs once at
  DEBUG (never raises into a caller).

**Files**
- New: `catalog/tmdb_index/index.py` (+ export in `__init__.py`)
- Possibly: extract `_norm`/`_titles_similar` to `enrichment/normalise.py` or a small
  `_textmatch.py` if import hygiene requires (keep behavior identical; no downstream change).

**Testing**
- Unit table-driven: exact match, article/case/number-word variants (`"The Thing"` vs
  `"thing"`, `"Se7en"`/`"Seven"`), a typo (`"Godzila"`), popularity tiebreak between two
  same-norm titles, and a confident **miss** (returns `None`).
- Unit: `original_title` query resolves a foreign film that the English title misses; result
  reports `matched_on="original_title"`.
- Unit: `extract_imdb_tt` — IMDb URL in a description, a bare `tt0083658`, a false positive
  (`tt` inside a word) rejected, multiple ids returns the first, no match → `None`.
- Unit: missing index file → `None`, no exception.

**Acceptance criteria**
- [ ] `resolve` returns the correct `tmdb_id` + confidence for exact/fuzzy cases and `None` for
      confident misses, offline, in the same process.
- [ ] `resolve` matches against `original_title` and reports `matched_on`.
- [ ] `extract_imdb_tt` pulls a `tt#` from IMDb URLs and bare ids, rejects false positives.
- [ ] Matching parity with existing `_titles_similar` semantics (no regression vs API-side
      matching for the shared cases).
- [ ] Tests green; `black`/`ruff`/`mypy` clean.

---

## Sortie 3 — `identify` step + identity-first art/meta (the tt# spine)

**Objective**: Stamp `tmdb_id` + `imdb_tt` onto every resolvable item once; make art/meta use it.

**Scope**
- Pipeline: insert `identify` after `classify`, before `title`
  (`ALL_STEPS = ["sync","classify","identify","title","meta","art","tags","categories"]` in
  [`pipeline.py`](../kryten_webqueue/catalog/enrichment/pipeline.py)). Wire into the `steps` enum used by jobs.
- New `catalog/enrichment/steps/identify.py` `IdentifyStep` — **accuracy-first waterfall**
  (PRD §5.3), stopping at the first authoritative/confident hit:
  - For each classification lacking cached identity (or `force`):
    1. `catalog.imdb_tt` present → ensure `tmdb_id` cached; `resolved_source="admin"`; done.
    2. `extract_imdb_tt(description, source/manifest URL, raw_title)` → hit → `search_by_imdb_id`
       (authoritative `/find`) → cache + promote; `resolved_source="scraped_url"|"scraped_desc"`.
       **Primary YouTube-rip mitigation.**
    3. Original-language title (from CMS record / secondary title) →
       `TMDBLocalIndex.resolve(..., original_title=...)` → hit `exact`/`high` → details fetch for
       `imdb_id` → cache + promote; `resolved_source="original_title"`.
    4. English title → `TMDBLocalIndex.resolve(lookup_title, lookup_year)`; `exact`/`high` →
       `fetch_by_tmdb_id` → cache + promote; `resolved_source="english_title"`.
       `low` → cache nothing to `catalog`; record `low_confidence`.
    5. Miss → existing `search_movie` once; success → cache + promote
       (`resolved_source="api_search"`); else record `no_local_match`.
  - **Auto-promotion is confirmed**: on any authoritative or `exact`/`high` hit, write
    `catalog.imdb_tt` (guard the v23 unique index; on collision skip + log + mark `ambiguous`)
    and an `item_edit_log` row carrying `resolved_source`. `low`-confidence never promotes.
  - Persist a per-item `identify` reason + `resolved_source` for the coverage report (Sortie 4).
  - `dry_run` → run the waterfall + record reasons but perform no writes (report mode).
- `ItemClassification` (in [`classify.py`](../kryten_webqueue/catalog/enrichment/classify.py)) gains `tmdb_id: str | None = None`,
  populated from `item_enrichment_state` when reconstructing cached classifications
  (`pipeline._classify_row`).
- `art` ([`steps/art.py`](../kryten_webqueue/catalog/enrichment/steps/art.py)): in `_resolve_poster`, if `cls.imdb_tt` or `cls.tmdb_id`
  is set, fetch poster **by id** (add `TMDBProvider.poster_by_tmdb_id` or reuse
  `fetch_by_tmdb_id().poster_url`) and skip the title search. Only fall back to fuzzy search
  when identity is absent.
- `meta` ([`steps/meta.py`](../kryten_webqueue/catalog/enrichment/steps/meta.py)): in `_lookup_movie`, when `imdb_tt` is absent but
  `cls.tmdb_id` is set, use `fetch_by_tmdb_id` instead of `search_movie`.

**High-stakes note**: this changes classify/identity logic — existing items only pick up the new
identity on a `force` re-run. `catalog.imdb_tt` auto-population is gated on
`exact`/`high` confidence and respects the unique index (see Open Question #2/#3 in the PRD —
confirm auto-promote vs admin-confirm before merge).

**Files**
- New: `catalog/enrichment/steps/identify.py`
- Edit: `pipeline.py`, `steps/__init__.py`, `classify.py`, `steps/art.py`, `steps/meta.py`,
  `providers.py` (add `fetch_by_tmdb_id`), enrichment DB helpers if a new
  `save_identity`/`get_identity` accessor is cleaner than `save_enrichment_state`.

**Testing**
- Unit: `IdentifyStep` with a mocked `TMDBLocalIndex` + mocked `TMDBProvider`:
  scraped `tt#` from description → `search_by_imdb_id` path, caches + promotes,
  `resolved_source="scraped_desc"`, audit-log row written.
- Unit: no `tt#`, original-title hit → promotes with `resolved_source="original_title"`.
- Unit: English-title `exact` hit → caches `tmdb_id`+`imdb_id`, promotes `imdb_tt`.
- Unit: `imdb_tt` collision → does not overwrite, marks ambiguous, logs.
- Unit: local miss → API fallback path invoked once; success caches, failure records reason.
- Unit: `low` confidence → no `catalog.imdb_tt` write.
- Unit: waterfall short-circuits — a scraped `tt#` means `resolve` is never called.
- Unit: `art._resolve_poster` with cached `tmdb_id`/`imdb_tt` → **no** title search performed
  (assert the search method is not called); poster fetched by id.
- Unit: `meta._lookup_movie` with `tmdb_id` but no `imdb_tt` → uses `fetch_by_tmdb_id`.
- Integration (dry_run): pipeline `steps=["classify","identify"]` over fixture rows → identity
  cached, no CMS writes.

**Acceptance criteria**
- [ ] `identify` step resolves + caches `tmdb_id`/`imdb_tt` for resolvable items; promotion
      gated on confidence and unique-index-safe with an audit trail.
- [ ] `art` performs zero title searches for items with cached identity; posters come from id.
- [ ] `meta` uses cached `tmdb_id` when `imdb_tt` is absent.
- [ ] `force` re-applies identity to already-classified items; non-force respects cache.
- [ ] Tests green; `black`/`ruff`/`mypy` clean; `CHANGELOG.md` updated (identity step is a
      versioned behavior change).

---

## Sortie 4 — Coverage / gap report (idea #6) + docs

**Objective**: Report which items resolve, which don't, and why — a cleanup worklist.

**Scope**
- `catalog/tmdb_index/coverage.py` (or extend `enrichment/report.py`): given the catalog +
  `item_enrichment_state`, emit per-item rows: `friendly_token`, `title`, `content_type`,
  `resolved`, `tmdb_id`, `imdb_tt`, `reason`
  (`resolved` | `no_local_match` | `foreign_title` | `ambiguous` | `low_confidence` |
  `non_movie`). Summary counts by reason.
- Expose via the `identify` step's `dry_run` (report-only) path and a job
  `tmdb_coverage_report` that writes the summary to run-history detail (JSON). Optional admin
  JSON view — **deferred** (note in docs).
- Docs: update `SPEC_CATALOG_ENRICHMENT_PIPELINE.md` to include the `identify` step in the
  pipeline order; add a short `docs/` note describing the local index + refresh workflow; ensure
  `config.example.json` documents the new keys.

**Testing**
- Unit: report classifies each reason correctly from crafted DB states (resolved item,
  unresolved-no-match, foreign, ambiguous/collision, non-movie).
- Unit: summary counts sum to the catalog size for the covered content types.

**Acceptance criteria**
- [ ] Coverage report lists every relevant catalog item with a resolved flag + reason and a
      summary by reason.
- [ ] `tmdb_coverage_report` job produces the summary in run history.
- [ ] Pipeline docs updated to show the `identify` step; config docs in sync.
- [ ] Tests green; `black`/`ruff`/`mypy` clean.

---

## Cross-cutting checks (every sortie)
- `uv run black . && uv run ruff check --fix . && uv run mypy kryten_webqueue && uv run pytest`.
- No new service-to-service HTTP, no raw `nats-py`, no hardcoded paths/subjects.
- New config keys mirrored in `config.example.json`; secrets never committed.
- `CHANGELOG.md` (Keep-a-Changelog + SemVer) updated for the versioned behavior change in
  Sortie 3 (and version bump in `pyproject.toml`).

## Deferred / follow-ups (not in this sprint)
- `download` source for `tmdb_index_refresh` (auto-pull daily gzip exports).
- Autocomplete / "did you mean" search off the local movie/tv index.
- Trivia seeds (kryten-economy) and fact seeding (kryten-llm) from keywords + popular titles.
- English-title alias source to close the foreign-title resolution gap.

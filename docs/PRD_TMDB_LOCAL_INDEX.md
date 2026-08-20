# PRD — Local TMDB Index & Identity-First Enrichment

**Status**: Draft
**Owner**: catalog / enrichment
**Related code**: `kryten_webqueue/catalog/enrichment/`, `kryten_webqueue/catalog/db/`, `kryten_webqueue/jobs/`
**Supersedes/extends**: `SPEC_CATALOG_ENRICHMENT_PIPELINE.md`
**Covers ideas**: #1 (local title→ID resolver), #2 (offline fuzzy/typo-tolerant matching), #6 (catalog coverage/gap analysis), plus the **tt#-for-every-item** identity goal.

---

## 1. Executive Summary

TMDB publishes **daily ID-export dumps** — newline-delimited JSON listing every movie,
TV series, person, company, keyword, and network on the service, with an ID, a name/title,
and a popularity score. We already download them (`TMDB_Raw_Data/data/`).

This PRD proposes building a **local SQLite index** from those dumps and using it to do the
one thing that most improves enrichment quality: **resolve every catalog item to a stable
`tmdb_id` and IMDb `tt#` once, cache it, and make every downstream step (art, meta, tags)
key off that identity instead of re-fuzzy-matching titles on each run.**

**North star: maximize accuracy with as little manual item editing as possible.** Every design
choice below is ranked by how many items it correctly identifies *without* an admin touching
them. Authoritative signals (an IMDb `tt#` scraped from the source, an original-language title)
are preferred over fuzzy English-title matching precisely because they need no human in the loop.

The dumps are shallow (IDs + names + popularity — no year, genre, cast, or synopsis), so they
are a **resolver/index**, not a metadata source. But an offline resolver + a single
details-by-id API call per item is enough to stamp a durable identity onto the catalog, which
is the strong key to art and metadata resolution.

## 2. Problem Statement

Today the enrichment pipeline resolves identity **implicitly and repeatedly**:

- `meta` fuzzy-searches TMDB by `lookup_title`/`lookup_year` on every run, unless an admin has
  hand-set `catalog.imdb_tt` (v23), in which case it does an authoritative `/find` lookup.
- `art` **always** fuzzy-searches by title ([`steps/art.py`](../kryten_webqueue/catalog/enrichment/steps/art.py)) — it never uses `imdb_tt`
  or the `tmdb_id` already cached in `item_enrichment_state` (v19). This is the main source of
  wrong posters on ambiguously-titled or hosted films.
- `meta` writes `tmdb_id`/`imdb_id` into `item_enrichment_state` as a side effect, but that
  identity is **never promoted** to `catalog.imdb_tt` and **never fed back** to `classify`, so
  `art` and subsequent runs can't benefit from it.

Consequences:
- Redundant `search/movie` API calls (one per item per step, plus year-less retries).
- Wrong matches on foreign titles, hosted/riffed films, and near-duplicate titles.
- No systematic way to know **which items we can't confidently identify** (coverage gap).

## 3. Goals & Success Metrics

**Goals**
1. **Local resolver**: `local_resolve(title, year) -> ResolveResult` returning a `tmdb_id`
   (+ confidence) with **no network call**, backed by the daily dumps. (Idea #1)
2. **Offline fuzzy/typo tolerance**: normalized-title matching + popularity tiebreak so we can
   disambiguate and reject junk before spending an API call. (Idea #2)
3. **tt# for every item**: a first-class `identify` step that resolves each catalog item to
   `tmdb_id` → `imdb_tt`, caches it, and makes `art`/`meta` prefer it. (identity spine)
4. **Coverage report**: a report of items that resolve / don't resolve, with reasons, to drive
   cleanup. (Idea #6)
5. **Daily refresh**: a single-flight job that rebuilds the local index from the dumps.

**Success metrics**
- ≥ 90% of `movie`/`hosted_movie`/`riffed_movie` catalog items carry a resolved `tmdb_id`
  after one `identify` run; ≥ 80% carry an `imdb_tt`.
- `art` and `meta` API `search/*` calls per full enrichment run drop measurably (target: art
  makes **zero** title searches for items with cached identity).
- Coverage report lists every unresolved item with a reason code.
- Index rebuild from local dumps completes in a bounded, logged time and is single-flight.

**Non-goals**
- Not a metadata replacement — year/genre/cast/synopsis still come from the TMDB/OMDB API.
- No new public HTTP endpoints beyond an admin coverage view (optional, deferred).
- No person/company/keyword features in this sprint (index them, but wiring them into
  trivia/autocomplete is out of scope here).

## 4. What the dumps actually are

| File (2026-08-19) | Records | Fields |
|---|---:|---|
| `movie_ids` | 1,234,178 | `id`, `original_title`, `popularity`, `adult`, `video` |
| `tv_series_ids` | 229,476 | `id`, `original_name`, `popularity` |
| `person_ids` | 4,877,524 | `id`, `name`, `popularity`, `adult` |
| `production_company_ids` | 257,083 | `id`, `name` |
| `keyword_ids` | 93,388 | `id`, `name` |
| `tv_network_ids` | 5,560 | `id`, `name` |

**Caveats (must be designed around):**
- `original_title`/`original_name` are the **native-language** titles (e.g. `プライド`,
  `L'Amour à vingt ans`). English catalog titles won't match these directly — the resolver
  will have a real miss rate on foreign films and must fall back to the API.
- `popularity` is TMDB's daily-decaying score — a good **tiebreak**, not a truth signal.
- Dumps are a **dated snapshot**; `person_ids` alone is 339 MB. The index must live outside
  the operational DB and be cheap to drop and rebuild.

## 5. Technical Architecture

### 5.1 Separate index database
The index lives in its **own SQLite file** (config `tmdb_index_path`, default
`/var/lib/kryten-webqueue/tmdb_index.db`), **not** in `webqueue.db`. Rationale:
- Keeps 1.2M+ movie rows and 4.8M person rows out of the operational DB and its migration
  chain (currently at v23).
- The index is a **derived cache** — we can drop and rebuild it freely without touching
  operational data or migrations.

Schema (rebuilt each refresh):
```
movies(tmdb_id INTEGER PRIMARY KEY, original_title TEXT, norm_title TEXT, popularity REAL, adult INT)
CREATE INDEX idx_movies_norm ON movies(norm_title);
movies_fts  -- FTS5 over norm_title, external-content on movies
tv(tmdb_id INTEGER PRIMARY KEY, original_name TEXT, norm_title TEXT, popularity REAL)
CREATE INDEX idx_tv_norm ON tv(norm_title);
tv_fts
people(tmdb_id INTEGER PRIMARY KEY, name TEXT, popularity REAL)      -- indexed, unused this sprint
keywords(tmdb_id INTEGER PRIMARY KEY, name TEXT)
companies(tmdb_id INTEGER PRIMARY KEY, name TEXT)
networks(tmdb_id INTEGER PRIMARY KEY, name TEXT)
index_meta(source_date TEXT, built_at TEXT, counts_json TEXT)
```
`norm_title` uses the **existing** `_norm()` from
[`enrichment/providers.py`](../kryten_webqueue/catalog/enrichment/providers.py) (article/case/punctuation stripping + number-word
mapping) so offline matching is consistent with the current `_titles_similar` logic.

### 5.2 Resolver
`TMDBLocalIndex.resolve(title, year=None, kind="movie") -> ResolveResult | None`:
1. `norm = _norm(title)`; exact `norm_title` match → candidates.
2. If none, FTS5 MATCH on `norm` → candidates.
3. Rank: exact-norm first, then `difflib` ratio ≥ threshold (reuse `_titles_similar`), then
   `-popularity`. Year is **not** in the dumps, so it can't filter here — it stays a
   downstream API disambiguator.
4. Return `{tmdb_id, matched_title, popularity, confidence}` or `None`.

`confidence`: `exact` (norm equality) | `high` (ratio ≥ 0.85) | `low` (matched but weak).

Because the dumps are keyed on **`original_title`**, `resolve` matches against **both** the
normalized `original_title` and (when present) an original-language query title — original-title
matches are the single highest-yield offline signal and are ranked above English-only matches.

### 5.2a IMDb `tt#` extraction (`extract_imdb_tt`)
A pure helper `extract_imdb_tt(*texts: str) -> str | None` that regex-scans arbitrary text for
an IMDb id: `imdb\.com/title/(tt\d{7,8})` or a delimited bare `\btt\d{7,8}\b`. Used by `identify`
against each item's `description`, source/manifest URL, and title. This is the highest-accuracy,
zero-matching path and the primary fix for YouTube full-movie rips (bad title, but an IMDb link
in the description). Returns the first valid `tt#` or `None`; never raises.

### 5.3 Identity-first enrichment (the tt# spine)
New pipeline step **`identify`**, inserted after `classify` and before `title`:
```
sync → classify → identify → title → meta → art → tags → categories
```
Identity is resolved by an **accuracy-first waterfall** — cheapest + most authoritative signals
first, fuzzy matching last, so the maximum number of items are correctly identified with zero
manual editing. For each classified item without a cached identity (or under `force`):

1. **Cached / admin `tt#`** — `catalog.imdb_tt` already set → identity known; ensure `tmdb_id`
   is cached, done. (No network beyond a details fetch if `tmdb_id` is missing.)
2. **Scraped `tt#` (authoritative, no matching)** — extract an IMDb `tt#` from the item's own
   text (`description`, source/manifest URL, title) via `extract_imdb_tt()`; e.g.
   `imdb.com/title/tt0083658` or a bare `tt0083658`. This is the **primary mitigation for
   YouTube full-movie rips**, which frequently paste an IMDb link in the description but have
   garbage titles. A scraped `tt#` → TMDB `GET /find?external_source=imdb_id` (authoritative;
   the existing `search_by_imdb_id`) → cache + **promote** to `catalog.imdb_tt`.
3. **Original-language title** — if the item carries an original-language title (from the CMS
   record, or a parenthetical/secondary title in the raw string), resolve it against the index's
   `original_title`/`original_name` column **before** the English title. In practice these match
   the dumps far better (the dumps are keyed on `original_title`), so this is a high-yield,
   low-false-positive path.
4. **English normalized title** — `local_resolve(lookup_title, lookup_year)` against `norm_title`
   (exact norm → FTS5 → ratio + popularity). Hit at `exact`/`high` confidence → details fetch
   for `imdb_id` → cache + promote. `low` confidence → do **not** promote; record for review.
5. **API fuzzy fallback** — miss → the current `search/movie` path once; on success cache +
   promote, else record a `no_local_match` reason.

`ItemClassification` gains a `tmdb_id` field so `art`/`meta` can key off cached identity.

**Auto-promotion is decided (was Open Question #2/#3):** `identify` **auto-populates**
`catalog.imdb_tt` for any identity resolved at `exact`/`high` confidence or from an authoritative
`tt#` (scraped or admin), because the north star is accuracy with minimal manual editing. Every
auto-promotion writes an `item_edit_log` row with a `resolved_source`
(`admin` | `scraped_url` | `scraped_desc` | `original_title` | `english_title` | `api_search`)
so admins can audit and, if ever wrong, correct. `low`-confidence fuzzy matches are **not**
promoted — they surface in the coverage report for optional review.

Downstream changes:
- `art`: prefer `imdb_tt`/`tmdb_id` → fetch poster by id; only fuzzy-search when identity is
  absent. (Fixes wrong hosted-movie posters.)
- `meta`: already prefers `imdb_tt`; also accept a cached `tmdb_id` to skip the search.

This is a **classify/identity logic change** — per AGENTS.md it only reaches already-processed
items on a `force` re-run.

### 5.4 Refresh job
New job `tmdb_index_refresh` (registered like other jobs, single-flight, run-history tracked):
- Params (schema-validated): `source` enum `local` (default) | `download`; `dump_dir` string
  (for `local`); `kinds` enum subset (default `movies,tv`).
- `local`: read JSONL from `dump_dir`; stream-parse line-by-line (never load whole file);
  rebuild the index in a temp DB then atomically swap. Runs off-loop via `asyncio.to_thread`.
- `download`: fetch TMDB's gzipped daily export URLs first, then build. (Deferred to a later
  sortie; `local` ships first since dumps are already present.)
- APScheduler cron entry (daily) via existing `job_scheduler.py`.

### 5.5 Coverage / gap report (Idea #6)
Report generator (reuse `enrichment/report.py` patterns) producing, per catalog item:
`friendly_token`, `title`, `content_type`, `resolved` (bool), `tmdb_id`, `imdb_tt`,
`reason` (`resolved` | `no_local_match` | `foreign_title` | `ambiguous` | `non_movie`).
Surfaced as: (a) a `--report`/dry-run mode on the `identify` step, and (b) optionally an admin
JSON view (deferred). Foreign-title items are the expected residual and are flagged, not errors.

## 6. Dependencies
- Existing: `aiosqlite`, `httpx`, the enrichment pipeline, `JobManager`, `_norm`/`_titles_similar`.
- New config keys: `tmdb_index_path`, `tmdb_index_source_dir`. Add to `config.example.json`.
- TMDB API key already required for the details-by-id call.

## 7. Security & Privacy
- Read-only consumption of local dump files; validate/limit `dump_dir` to a configured path
  (no arbitrary path traversal from job params).
- No secrets in the index. TMDB attribution terms apply — index is internal-only.
- `imdb_tt` promotion must respect the v23 unique index: on collision, **do not** overwrite an
  existing item's tt#; log and mark ambiguous.

## 8. Rollout Plan
1. Sortie 1 — local index schema + builder + `tmdb_index_refresh` job (`local` source).
2. Sortie 2 — `TMDBLocalIndex.resolve` + tests (ideas #1/#2).
3. Sortie 3 — `identify` step + `ItemClassification.tmdb_id` + art/meta wiring (tt# spine).
4. Sortie 4 — coverage/gap report (idea #6) + docs + `config.example.json`.

Ship behind normal enrichment gating; `identify` is opt-in via the `steps` param until proven,
then added to the default `all` sequence. Reprocessing existing items requires `force`.

## 9. Future Enhancements
- `download` source for the refresh job (auto-pull daily dumps).
- Autocomplete / "did you mean" for webqueue search off the local movie/tv index.
- Trivia seeds for kryten-economy / fact seeding for kryten-llm from keywords + popular titles.
- Person/company indexing wired into credits validation.

## 10. Decisions & Open Questions

**Decided**
1. **Index DB location** — ✅ **Separate** `tmdb_index.db` (same server, distinct DB file /
   schema). Keeps the operational DB's v23 migration chain clean; the derived index can be
   dropped and rebuilt freely.
2. **tt# auto-promotion** — ✅ **Auto-populate** `catalog.imdb_tt` from resolved identity. The
   north star is accuracy with minimal manual editing, so we promote rather than queue for admin
   confirmation. Every write logs an `item_edit_log` row with `resolved_source` for audit.
3. **Confidence gate** — ✅ Promote on **authoritative** signals (scraped/admin `tt#`) and
   `exact`/`high` title confidence; **never** promote `low`-confidence fuzzy matches (they go to
   the coverage report for optional review).
4. **Resolution order** — ✅ Accuracy-first waterfall (PRD §5.3): admin `tt#` → scraped `tt#` →
   original-language title → English title → API fuzzy fallback.

**Open**
5. **Foreign-title residual** — after original-title matching + `tt#` scraping, accept the
   remaining miss and rely on API fallback, or add an English-title alias source later?
   (Out of scope this sprint; flagged in the coverage report.)
6. **`tt#` scrape source breadth** — description + source/manifest URL + title covers YouTube
   rips. Do we also re-fetch the MediaCMS media detail page during `sync` to capture a richer
   original description? (Deferred; revisit if description coverage proves thin.)

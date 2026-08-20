# kryten-webqueue — Unified Catalog Enrichment Pipeline

**Version:** 1.0  
**Date:** 2026-08-13  
**Status:** Ready for implementation  

---

## 0. Decisions Captured

| # | Question | Decision |
|---|----------|----------|
| 1 | Source of truth | Enrichment writes through webqueue → CMS. CMS is authoritative only for categories/tags that don't yet exist in webqueue (synced inward on the `tags` step). |
| 2 | Tag/category push rules | TMDB genres → tags; hosted-show → tag; MPAA rating → tag; category auto-assignment deferred to a later sortie. |
| 3 | Art for hosted movies | Use `hosted.movie_title` + `hosted.movie_year` for the TMDB/OMDB query; store the film's actual poster; keep the original CMS display title unchanged. |
| 4 | MST3K vs Rifftrax | Separate cases. MST3K → `riffed_movie` with its own show entry. Rifftrax → `riffed_movie` with title reformat `"RiffTrax Presents: Title (Year)"`. |
| 5 | Force re-fetch art | In force mode: re-query TMDB/OMDB. Before downloading, HTTP HEAD the candidate URL; if `Content-Length` matches the existing file on disk, skip the download. |
| 6 | Architecture | Option A: single `CatalogEnrichmentPipeline` with composable step flags. Old job wrappers become thin pass-throughs. |
| 7 | Scheduling | Unchanged — admin cron panel manages schedules with arbitrary params. |

---

## 1. Executive Summary

Three independent tools — `enrichtitles`, `enrichmeta`, `enrichtv` — plus the sync-time logic inside `sync.py` and `images.py` all perform overlapping title manipulation, TMDB/OMDB lookups, and CMS API calls without sharing state. Each tool re-paginates the full CMS catalog independently. Hosted-show detection lives only in `enrichmeta.py`, but the art resolver in `images.py` needs it too and does not have it, so every Svengoolie, MonsterVision, and Last Drive-In item fails poster lookup because the query includes the show name.

This spec consolidates all enrichment work into a single `CatalogEnrichmentPipeline` with discrete, named steps. Each step can run independently or as part of a full run. A persisted `item_enrichment_state` table in SQLite ensures that later steps inherit the classification and metadata found by earlier ones, eliminating redundant API calls and enabling reliable per-item forced re-runs.

---

## 2. Problem Statement

### 2.1 Duplicated Title Logic

| Location | What it does |
|----------|--------------|
| `sync.py` `_process_item` | `_normalize_leading_year`, `_strip_extension` |
| `images.py` `_clean_title` | Year extraction, noise stripping, extension stripping |
| `enrichtitles.py` | Dots→spaces, `[year]`→`(year)`, scene tags, extension strip |
| `enrichmeta.py` `parse_standard_title` | Scene tags, year extraction, dot expansion |
| `enrichmeta.py` `detect_hosted` | Hosted show stripping, year extraction |

Five separate implementations of what is fundamentally the same concern. A bug fix in one has never propagated to the others.

### 2.2 Art Resolver Ignores Hosted Context

`CoverArtResolver.resolve(token, title, db)` receives the raw stored title — `"Phantom of the Mall Svengoolie"` — and queries TMDB for exactly that string. TMDB returns nothing. The `enrichmeta.py` `detect_hosted` function correctly extracts `"Phantom of the Mall (1989)"`, but that information is never passed to the art resolver.

### 2.3 No Shared Enrichment State

Each job independently paginate the entire CMS catalog, classifies items from scratch, and performs its own TMDB/OMDB calls. A full run of `enrichtitles` + `enrichmeta` + `enrichtv` hits the CMS API approximately three times per item. TMDB/OMDB results are never cached between jobs, so `meta` and `art` both perform independent TMDB searches for the same film.

### 2.4 No Single-Item Force Path

There is no way to force a full re-run of all enrichment for one specific item. The admin must manually adjust CMS data to clear quality scores, wait for the next cron run, and hope all jobs execute in the right order.

### 2.5 Tags / Genres Never Pushed

TMDB provides genres, MPAA ratings, and structured metadata. None of this is propagated back to CMS as searchable tags. Hosted-show membership (Svengoolie, Last Drive-In, etc.) is embedded in the description `"Hosted Version: ..."` line but never as a searchable tag. Users cannot filter by genre or hosted show.

---

## 3. Architecture

### 3.1 Overview

```
CatalogEnrichmentPipeline.run(steps, tokens, force, dry_run)
    │
    ├── Step: sync      Pull new/updated items from CMS → local SQLite
    │                   (existing CatalogSync, unchanged interface)
    │
    ├── Step: classify  Classify each item: content type, hosted-show detection,
    │                   extract lookup_title / lookup_year for downstream steps.
    │                   Writes ItemClassification → item_enrichment_state.
    │
    ├── Step: identify  Resolve each item to a stable tmdb_id + IMDb tt# via an
    │                   accuracy-first waterfall (admin tt# → scraped tt# →
    │                   original-language title → English title → API search).
    │                   Auto-promotes tt# to catalog.imdb_tt on confident hits.
    │                   See SPEC_TMDB_LOCAL_INDEX.md.
    │
    ├── Step: title     Normalize / reformat titles; write-through to CMS.
    │                   Rules differ per content type (see §4.3).
    │
    ├── Step: meta      TMDB + OMDB lookup using classification.lookup_title.
    │                   Prefers cached imdb_tt / tmdb_id (from identify) over search.
    │                   Build structured description. Write-through to CMS.
    │                   Cache MovieMetadata (including poster_url) in enrichment state.
    │
    ├── Step: art       Resolve and download poster art.
    │                   Prefers cached imdb_tt / tmdb_id (from identify) over search.
    │                   Reads cached poster_url from enrichment state when available.
    │                   Writes to local images dir only (not CMS).
    │
    ├── Step: tags      Push TMDB genres, MPAA rating, hosted-show tag → CMS.
    │                   Sync any CMS-only tags → local catalog_tags (reverse direction).
    │
    └── Step: categories  [DEFERRED] Auto-assign CMS categories from content type + genres.
```

### 3.2 Data Flow

Classification is the linchpin. Every downstream step reads `item_enrichment_state` to find `lookup_title`, `lookup_year`, `hosted_show`, and cached `meta_json` rather than re-deriving them.

```
sync writes:           catalog (title, duration_sec, thumbnail_url, ...)
classify reads:        catalog; writes: item_enrichment_state
title reads:           catalog + enrichment_state; writes: catalog (local) + CMS
meta reads:            enrichment_state; writes: enrichment_state (meta_json, tmdb_id, imdb_id)
                       + catalog (description) + CMS
art reads:             enrichment_state (lookup_title, meta_json.poster_url);
                       writes: catalog (cover_art_path, cover_art_source)
tags reads:            enrichment_state (meta_json.genres, content_rating, hosted_show);
                       writes: CMS tags + catalog_tags (inward sync)
```

### 3.3 Pipeline Invocation

```python
pipeline = CatalogEnrichmentPipeline(db=db, config=config, cover_art=cover_art)

# Nightly full run
await pipeline.run()

# Art only (all items)
await pipeline.run(steps=["art"])

# Force full re-run for one item
await pipeline.run(tokens=["abc123def"], force=True)

# Force art re-fetch for one item
await pipeline.run(steps=["art"], tokens=["abc123def"], force=True)

# Dry run — preview what would change, no writes
await pipeline.run(dry_run=True)

# Limit for testing
await pipeline.run(steps=["classify", "meta"], limit=10)
```

### 3.4 Step Skip Logic

Each step records `last_{step}_at` in `item_enrichment_state`. The pipeline skips an item for a given step if:
- `last_{step}_at` is set **and** `force=False`
- For `meta`: also skip if `description_score >= min_score` and `force=False`
- For `art`: also skip if `cover_art_source in ('tmdb', 'omdb')` and `force=False`

`force=True` bypasses all skip checks for the selected steps.

---

## 4. Hosted Show Registry

The registry is the single authoritative source for all hosted-show knowledge. It replaces scattered `_HOSTED_PATTERNS` lists across the codebase.

### 4.1 Registry Schema

```python
@dataclass
class HostedShowEntry:
    pattern: re.Pattern          # match against raw CMS title
    show_name: str               # canonical display name
    content_type: str            # "hosted_movie" or "riffed_movie"
    title_treatment: str         # "keep", "reformat", or "strip_show"
    reformat_template: str | None  # e.g. "RiffTrax Presents: {title} ({year})"
    cms_tag: str                 # tag pushed to CMS for this show
```

### 4.2 Registry Entries

| Match Pattern | show_name | content_type | title_treatment | cms_tag |
|---|---|---|---|---|
| `(?:The\s+)?Last\s+Drive[\s\-]*In` | The Last Drive-In with Joe Bob Briggs | hosted_movie | keep | lastdrivein |
| `JBBTLDI\|Joe\s*Bob\s+TLDI` | The Last Drive-In with Joe Bob Briggs | hosted_movie | keep | lastdrivein |
| `Joe\s*Bob'?s?\s+Drive[\s-]*In\s+Theater` | Joe Bob's Drive-In Theater | hosted_movie | keep | joebobbriggs |
| `Monster\s*Vision` | MonsterVision with Joe Bob Briggs | hosted_movie | keep | monstervision |
| `Svengoolie` | Svengoolie | hosted_movie | keep | svengoolie |
| `Riff\s*Trax(?:\s+Live)?` | RiffTrax | riffed_movie | reformat | rifftrax |
| `MST3K\|Mystery\s*Science\s*Theater` | Mystery Science Theater 3000 | riffed_movie | keep | mst3k |

**`keep`**: the original CMS title is preserved verbatim (e.g., `"Phantom of the Mall Svengoolie"`). The `lookup_title` is the extracted film title used for API queries and art.

**`reformat`**: the CMS title is rewritten to `reformat_template` (Rifftrax only). The displayed title becomes `"RiffTrax Presents: Phantom of the Mall (1989)"`.

**`strip_show`**: the show name is removed but no template applied. Reserved for future entries.

### 4.3 Title Treatment Rules by Content Type

| `content_type` | Title treatment |
|---|---|
| `movie` | Normalize year position, strip extensions, remove scene tags → push to CMS if changed |
| `tv_episode` | Normalize to `"Show Name - S01E02 - Episode Title"` → push to CMS if changed |
| `hosted_movie` | **No title change.** Keep original CMS title. |
| `riffed_movie` | Apply `reformat_template` if set; otherwise keep. |
| `archive` | No title changes. |
| `unknown` | No title changes. |

---

## 5. Data Model

### 5.1 `ItemClassification`

Produced by the classify step; consumed by all subsequent steps.

```python
@dataclass
class ItemClassification:
    friendly_token: str
    raw_title: str               # as-stored in local catalog
    content_type: str            # movie | tv_episode | hosted_movie | riffed_movie | archive | unknown
    hosted: HostedInfo | None    # set for hosted_movie and riffed_movie
    lookup_title: str            # title used for TMDB/OMDB queries
    lookup_year: str | None      # year extracted from title
    genre_hints: list[str]       # from YouTube "Full Movie | Action Drama" pattern
    tv_show: str | None          # for tv_episode: extracted show name
    tv_season: int | None
    tv_episode: int | None
    duration_sec: int
    description_score: int       # current quality score from score_description()
    has_real_art: bool           # cover_art_source in ('tmdb', 'omdb')
```

### 5.2 `EnrichmentState` (DB row)

```sql
CREATE TABLE item_enrichment_state (
    friendly_token   TEXT PRIMARY KEY,
    content_type     TEXT,
    hosted_show      TEXT,         -- show_name from HostedShowEntry, or NULL
    lookup_title     TEXT,
    lookup_year      TEXT,
    tv_show          TEXT,
    tv_season        INTEGER,
    tv_episode_num   INTEGER,
    description_score INTEGER,
    tmdb_id          TEXT,
    imdb_id          TEXT,
    meta_json        TEXT,         -- JSON-serialised MovieMetadata (includes poster_url)
    last_classify_at TEXT,
    last_title_at    TEXT,
    last_meta_at     TEXT,
    last_art_at      TEXT,
    last_tags_at     TEXT
);
```

**`meta_json`** is the cache keystone. If meta has already run and `meta_json` is populated, the art step reads `poster_url` from it instead of making a second TMDB call.

### 5.3 `MovieMetadata`

Unified dataclass replacing the separate versions in `enrichmeta.py` and `images.py`. Adds `poster_url` so both meta and art share a single provider call.

```python
@dataclass
class MovieMetadata:
    title: str = ""
    year: str | None = None
    synopsis: str = ""
    director: list[str] = field(default_factory=list)
    producer: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    content_rating: str = ""        # MPAA: G, PG, PG-13, R, NC-17
    runtime_min: int | None = None
    tagline: str = ""
    imdb_rating: str = ""
    imdb_id: str = ""
    tmdb_id: str = ""
    rotten_tomatoes: str = ""
    metacritic: str = ""
    tmdb_rating: str = ""
    writer: list[str] = field(default_factory=list)
    cinematographer: list[str] = field(default_factory=list)
    composer: list[str] = field(default_factory=list)
    editor: list[str] = field(default_factory=list)
    special_effects: list[str] = field(default_factory=list)
    poster_url: str | None = None   # TMDB or OMDB poster URL

    @property
    def found(self) -> bool:
        return bool(self.synopsis or self.cast or self.director)
```

### 5.4 Provider Abstraction

Both meta and art steps use the same provider interface. HTTP clients are async (`httpx.AsyncClient`).

```python
class MetadataProvider(Protocol):
    async def search_movie(
        self, title: str, year: str | None = None
    ) -> MovieMetadata: ...

    async def search_tv_episode(
        self, show: str, season: int, episode: int
    ) -> MovieMetadata: ...

    async def close(self) -> None: ...
```

Implementations: `TMDBProvider`, `OMDBProvider`. A `merge_metadata(tmdb, omdb)` function combines them (same merge logic as current `enrichmeta.py`, consolidated here).

---

## 6. Step Specifications

### 6.1 Step: `sync`

**Purpose:** Pull catalog from CMS into local SQLite.

**Behaviour:** Existing `CatalogSync` interface is preserved. One addition: for any item not present in `item_enrichment_state`, insert a skeleton row with `last_classify_at = NULL` so the classify step picks it up.

**Trigger:** On every run that includes `sync` (nightly default). Can be omitted for pure enrichment runs.

---

### 6.2 Step: `classify`

**Purpose:** Determine what kind of thing each catalog item is and extract the clean lookup title for all downstream steps.

**Input:** `catalog` table (raw titles, durations, current art source).

**Output:** `item_enrichment_state` (content_type, hosted_show, lookup_title, lookup_year, description_score, last_classify_at).

**Classification rules (evaluated in order):**

1. **Hosted movie** — raw title matches any `HostedShowEntry.pattern` → `hosted_movie`. Extract film title/year via `detect_hosted()`.
2. **Riffed movie** — title matches Rifftrax or MST3K patterns → `riffed_movie`. Extract film title/year.
3. **TV episode** — title matches `_TV_EPISODE_RE` (S01E02, SxxExx, NxNN, "Season N", "Episode N") → `tv_episode`. Extract show name, season, episode.
4. **Archive** — duration > 1800 s but title matches archive patterns (broadcast date stamps, wrestling promotions, "WWF/WCW/NWA + date", "MonsterVision [date]", "Complete Broadcast") → `archive`.
5. **Short** — duration < 600 s (below 10-minute threshold) → classify as `unknown` (handled as-is; no enrichment).
6. **Movie** — duration >= 1800 s, none of the above → `movie`. Apply `parse_standard_title()`.
7. **Unknown** — anything else.

**Does NOT modify titles.** Classification is a read-only analytical step.

---

### 6.3 Step: `title`

**Purpose:** Clean and canonicalize titles; write-through to CMS for items that change.

**Input:** `catalog` + `item_enrichment_state`.

**Processing per content type:**

- **`movie`:** Apply `normalize_and_clean(raw_title)` → `(clean_title, year)`. If clean_title differs from raw: update local DB + push to CMS via `PUT /api/v1/media/{token}`.
  - `normalize_and_clean` = shared helper (replacing all five current implementations): strip extension → normalize leading year → replace dots/underscores with spaces → strip scene tags → strip noise brackets → strip trailing junk.
- **`tv_episode`:** Reformat to canonical `"Show Name - S01E02 - Episode Title"` if not already. Push to CMS if changed.
- **`riffed_movie` (Rifftrax):** Reformat to `"RiffTrax Presents: {movie_title} ({year})"`. Push to CMS.
- **`riffed_movie` (MST3K):** Title already formatted; keep. No push.
- **`hosted_movie`:** No change. Write `last_title_at` timestamp.
- **`archive`, `unknown`:** No change. Write `last_title_at` timestamp.

**CMS write note:** `PUT /api/v1/media/{token}` overwrites the owner with the API token user (known MediaCMS bug). After each CMS write, call `POST /api/v1/media/user/bulk_actions` with `change_owner` to restore the original uploader.

---

### 6.4 Step: `meta`

**Purpose:** Fetch rich structured metadata (synopsis, cast/crew, ratings, genres) from TMDB + OMDB; build a quality description; write-through to CMS.

**Input:** `item_enrichment_state` (lookup_title, lookup_year, hosted_show).

**Skip condition (normal mode):** `description_score >= min_score` (default 50). Run with `force=True` to re-enrich all items regardless.

**Processing:**

1. Query `TMDBProvider.search_movie(lookup_title, lookup_year)` — TMDB returns metadata AND `poster_url`.
2. If TMDB found `imdb_id`: query `OMDBProvider.search_movie(imdb_id=imdb_id)` for ratings.
3. Else: query `OMDBProvider.search_movie(lookup_title, lookup_year)` as fallback.
4. `merge_metadata(tmdb_meta, omdb_meta)` → unified `MovieMetadata`.
5. `format_description(meta, hosted=classification.hosted)` → structured description text.
6. Push description (and canonical title for Rifftrax if not done in title step) to CMS.
7. Serialise `MovieMetadata` → `meta_json`; update `enrichment_state` (tmdb_id, imdb_id, last_meta_at, description_score).
8. Update local `catalog.description`.

**TV episodes:** Route to TV-specific enrichment. Show lookups are cached per-show name in memory for the duration of the step run (same series hit TMDB once).

**Description format for hosted movies:**

```
Hosted Version: Svengoolie

Synopsis:
[film synopsis]

Release Year: 1958
...
```

---

### 6.5 Step: `art`

**Purpose:** Fetch and cache cover art (poster) for catalog items.

**Input:** `item_enrichment_state` (lookup_title, lookup_year, meta_json.poster_url). `catalog` (cover_art_source, cover_art_path).

**Skip condition (normal mode):** `cover_art_source in ('tmdb', 'omdb')`. Run with `force=True` to re-check even items with real art.

**Processing:**

1. **Read cached poster URL first.** If `meta_json` is populated and `meta_json.poster_url` is set, use that URL directly — no new TMDB call needed. Saves one API call per item when meta already ran.
2. **If no cached URL:** query TMDB using `lookup_title` + `lookup_year` (not the raw CMS title). This is the key fix for hosted movies.
3. **OMDB fallback:** if TMDB returns nothing, try OMDB.
4. **Thumbnail last resort:** if no poster found from any provider, fall back to MediaCMS thumbnail_url (cover_art_source = 'thumbnail').
5. **File-size check (force mode):** Before downloading, issue `HEAD {candidate_url}`. Read `Content-Length`. If `existing_file.stat().st_size == Content-Length`, skip download (same image). If Content-Length is 0 or absent, always download.
6. **Normal (non-force) mode:** if `cover_art_source in ('tmdb', 'omdb')` — skip entirely.
7. **Download + save responsive widths** (200w, 400w, 800w `.webp`).
8. Update `catalog.cover_art_path`, `catalog.cover_art_source`. Write `last_art_at`.

**No CMS write.** Art is served by webqueue's static files, not stored in MediaCMS.

---

### 6.6 Step: `tags`

**Purpose:** Push enriched metadata back to CMS as tags; sync new CMS-only tags into local DB.

**Input:** `item_enrichment_state` (meta_json.genres, meta_json.content_rating, hosted_show). `catalog_tags` (current local tags).

**Processing:**

1. **Genres → tags.** For each TMDB genre in `meta_json.genres`, normalize to a lowercase slug (e.g., `"Science Fiction"` → `sciencefiction`). If not already in CMS for this item, append via `POST /api/v1/media/{token}/tags`.
2. **Hosted-show → tag.** If `hosted_show` is set, push the `cms_tag` from the `HostedShowEntry` (e.g., `svengoolie`, `lastdrivein`).
3. **MPAA rating → tag.** If `content_rating` is a known MPAA value (`G`, `PG`, `PG-13`, `R`, `NC-17`, `NR`), push the slug (e.g., `mpaa-r`).
4. **Reverse sync.** Query CMS for the item's current tag set. Any tag present in CMS but not in the local `catalog_tags` join table is inserted locally (partial reverse-sync — only for existing items, not bulk CMS-only discovery).
5. Write `last_tags_at`.

**Tag normalisation rules:**
- Lowercase, spaces → stripped (not hyphenated — keep consistent with existing tag style)
- `"Science Fiction"` → `sciencefiction`
- `"Horror"` → `horror`
- Hosted-show tags follow the registry's `cms_tag` field exactly (already lowercase, no spaces)
- MPAA tags use `mpaa-` prefix to namespace them: `mpaa-pg13`, `mpaa-r`

---

### 6.7 Step: `categories` (DEFERRED)

**Purpose:** Auto-assign CMS categories from content type and genre data.

**Approach (planned):** Configuration-driven rules in `config.json` under `enrichment.category_rules`:

```json
"category_rules": [
  { "hosted_show": "svengoolie",         "category": "Svengoolie" },
  { "hosted_show": "monstervision",      "category": "MonsterVision" },
  { "hosted_show": "lastdrivein",        "category": "The Last Drive-In" },
  { "content_type": "tv_episode",        "category": "Television" },
  { "genre": "Documentary",              "category": "Documentary" },
  { "genre": "Animation",               "category": "Animation" }
]
```

Rules are evaluated in order; first match wins. Implementation is deferred.

---

## 7. File Layout

```
kryten_webqueue/catalog/
│
├── sync.py                      # Existing CatalogSync (unchanged interface)
│
├── images.py                    # CoverArtResolver (DEPRECATED interface kept for
│                                #   backward compat; art step is the new owner)
│
└── enrichment/
    ├── __init__.py              # exports CatalogEnrichmentPipeline
    ├── pipeline.py              # orchestrator; step dispatch; force/dry-run logic
    ├── classify.py              # ItemClassifier, ItemClassification, HOSTED_SHOW_REGISTRY,
    │                            #   detect_hosted(), parse_standard_title(), parse_tv_title()
    ├── providers.py             # TMDBProvider, OMDBProvider, MovieMetadata, merge_metadata()
    │                            #   (async httpx — replaces sync requests in enrichmeta.py)
    ├── normalise.py             # normalize_and_clean(), _strip_extension(),
    │                            #   _normalize_leading_year() — single canonical copy
    ├── report.py                # EnrichmentReport dataclass + summary formatting
    └── steps/
        ├── __init__.py
        ├── sync.py              # thin wrapper: calls catalog.sync.CatalogSync
        ├── title.py             # title normalisation → CMS write-through
        ├── meta.py              # TMDB+OMDB lookup → description → CMS write-through
        ├── art.py               # poster resolution + file-size check
        ├── tags.py              # genre/MPAA/hosted-show tags → CMS + reverse sync
        └── categories.py        # stub (deferred)
```

---

## 8. Job Interface

### 8.1 New Main Entry Point

```python
# kryten_webqueue/jobs/tasks.py

async def catalog_enrich_job(params: dict, ctx) -> dict:
    """Unified catalog enrichment pipeline.

    params:
      steps:      comma-separated list, or "all" (default)
      tokens:     comma-separated friendly_tokens, or "all" (default)
      force:      "1" | "true" to bypass cached state
      dry_run:    "1" | "true" to preview without writing
      limit:      integer, max items per step
      min_score:  integer, description quality threshold (default 50)
    """
    pipeline = CatalogEnrichmentPipeline(
        db=ctx.app.state.db,
        config=ctx.config,
        cover_art=ctx.app.state.cover_art,
    )
    step_param = params.get("steps", "all")
    steps = None if step_param == "all" else [s.strip() for s in step_param.split(",")]
    token_param = params.get("tokens", "all")
    tokens = None if token_param == "all" else [t.strip() for t in token_param.split(",")]

    report = await pipeline.run(
        steps=steps,
        tokens=tokens,
        force=params.get("force", "").lower() in ("1", "true"),
        dry_run=params.get("dry_run", "").lower() in ("1", "true"),
        limit=int(params["limit"]) if params.get("limit") else None,
        min_score=int(params.get("min_score", 50)),
        ctx=ctx,
    )
    return report.to_dict()
```

### 8.2 Backward-Compatible Wrappers

Existing job registrations keep working with their current cron schedules and params. Each is a thin pass-through:

```python
async def enrichtitles_job(params: dict, ctx):
    params.setdefault("steps", "classify,title")
    return await catalog_enrich_job(params, ctx)

async def enrichmeta_job(params: dict, ctx):
    params.setdefault("steps", "classify,meta")
    return await catalog_enrich_job(params, ctx)

async def enrichtv_job(params: dict, ctx):
    params.setdefault("steps", "classify,meta")
    return await catalog_enrich_job(params, ctx)

async def catalog_art_job(params: dict, ctx):
    params.setdefault("steps", "art")
    return await catalog_enrich_job(params, ctx)

async def catalog_tags_job(params: dict, ctx):
    params.setdefault("steps", "tags")
    return await catalog_enrich_job(params, ctx)
```

### 8.3 Admin UI — Single-Item Force Run

The item detail admin panel gains a **"Re-enrich"** button. It calls:

```
POST /admin/catalog/{token}/enrich
{
  "steps": "classify,title,meta,art,tags",
  "force": true
}
```

This fires `catalog_enrich_job` with `tokens=[token]` and `force=True`, returning a job_run ID for status polling. Gives operators a one-click full re-run for any individual item without touching the cron schedule.

---

## 9. `EnrichmentReport`

```python
@dataclass
class EnrichmentReport:
    steps_run: list[str]
    total_items: int
    by_step: dict[str, StepResult]
    elapsed_sec: float
    dry_run: bool

@dataclass
class StepResult:
    processed: int
    changed: int
    skipped: int
    failed: int
    errors: list[str]      # up to 20 representative errors
```

`report.to_dict()` for job context `progress()` updates, so the admin panel shows per-step counts as the pipeline runs.

---

## 10. DB Migration

Migration v18 — added alongside the first pipeline sortie:

```sql
CREATE TABLE IF NOT EXISTS item_enrichment_state (
    friendly_token   TEXT PRIMARY KEY,
    content_type     TEXT,
    hosted_show      TEXT,
    lookup_title     TEXT,
    lookup_year      TEXT,
    tv_show          TEXT,
    tv_season        INTEGER,
    tv_episode_num   INTEGER,
    description_score INTEGER,
    tmdb_id          TEXT,
    imdb_id          TEXT,
    meta_json        TEXT,
    last_classify_at TEXT,
    last_title_at    TEXT,
    last_meta_at     TEXT,
    last_art_at      TEXT,
    last_tags_at     TEXT
);
```

No data migration required. The classify step populates rows on first run; all timestamps start NULL (treated as "not yet run").

---

## 11. Migration Path

| Sortie | Work | Old code retired? |
|--------|------|-------------------|
| **1** | DB migration v18. `enrichment/` scaffold: `classify.py`, `normalise.py`, `providers.py` (async). Tests for classify + providers. | No |
| **2** | `steps/title.py` + tests. Register `catalog_enrich_job` with steps=classify,title. Wrapper replaces `enrichtitles_job` logic. | `enrichtitles.py` title logic retired |
| **3** | `steps/meta.py` + tests. Wrapper replaces `enrichmeta_job` + `enrichtv_job` logic. | `enrichmeta.py` + `enrichtv.py` retired |
| **4** | `steps/art.py` + tests. File-size check. Hosted-title art fix. Wrapper replaces old `CoverArtResolver` calls. | `images.py` `CoverArtResolver` retired |
| **5** | `steps/tags.py` + tests. New `catalog_tags_job`. Admin panel "Re-enrich" button. | — |
| **6** | `steps/categories.py` implementation (deferred). | — |

Vendored scripts (`enrichtitles.py`, `enrichmeta.py`, `enrichtv.py`) are retained as source reference until their sortie retires them. They must not be modified once Sorties 2–3 land; all fixes go to the new step modules.

---

## 12. Testing Strategy

| File | What it covers |
|------|---------------|
| `tests/test_classify.py` | Each `HostedShowEntry` pattern; TV episode patterns; archive detection; movie classification; `ItemClassification` field correctness |
| `tests/test_providers.py` | Mock TMDB + OMDB HTTP responses; `MovieMetadata` field mapping; `merge_metadata()` priority rules; year-mismatch retry logic |
| `tests/test_pipeline_title.py` | Title normalisation per content type; CMS write-through (mock); owner restoration call |
| `tests/test_pipeline_meta.py` | Hosted-movie description format; TV episode description format; quality score gate; `meta_json` caching |
| `tests/test_pipeline_art.py` | Hosted-movie uses `lookup_title` not raw title; file-size check skips when sizes match; thumbnail fallback; force mode overrides |
| `tests/test_pipeline_tags.py` | Genre normalisation; MPAA tag naming; hosted-show tag push; reverse-sync new CMS tags to local DB |
| `tests/test_pipeline_integration.py` | Full pipeline run against an in-memory DB; step ordering; force=True bypasses caches; dry_run produces no writes |
| `tests/test_title_normalization.py` | Migrated to test `normalise.normalize_and_clean()` — same assertions, new target |

---

## 13. Configuration

All enrichment config lives in `config.json` under an `"enrichment"` key (new; not currently present). Config auto-discovery order is unchanged.

```json
{
  "enrichment": {
    "min_score": 50,
    "min_duration_sec": 3600,
    "request_delay_sec": 0.25,
    "tmdb_api_key": "...",
    "omdb_api_key": "...",
    "category_rules": []
  }
}
```

`tmdb_api_key` and `omdb_api_key` are already present at the top level of config; both locations are accepted (top-level takes precedence for backward compat).

---

## 14. Open Questions / Deferred

1. **Category rules config** — the rule structure above is a placeholder. Category assignment logic will be specced once Sortie 5 is complete and the tag picture is clearer.
2. **MST3K title extraction** — MST3K episode titles vary widely (`"MST3K 910: The Final Sacrifice (FULL MOVIE)"`, `"The Giant Spider Invasion - MST3K"`). A dedicated parser will be needed and should be specced as a sub-task of Sortie 3.
3. **Archive exclusion list** — wrestling, broadcast recordings, and channel-produced content may warrant a configurable `archive_patterns` list in config rather than hard-coded regexes.
4. **OMDB poster quality** — OMDB poster URLs are often lower resolution than TMDB. The art step should prefer the TMDB poster when both are available, even in force mode.
5. **Rate-limit budgeting** — a full catalog run with ~5,200 items touches TMDB twice per movie (search + credits) plus OMDB once, totalling ~15,000 API calls. The existing backoff logic handles 429s but the step should report projected rate-limit wait time in the `EnrichmentReport`.

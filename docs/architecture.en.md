[中文](architecture.md) | [English](architecture.en.md) | [日本語](architecture.ja.md)  
[README](../README.en.md) | [Deployment and Usage Guide](guide.en.md) | [Architecture and Database](architecture.en.md)

# AnimeMachine Architecture

AnimeMachine is organized around explicit evidence and write boundaries. Torrent Collector may discover and write new `.torrent` metadata, while AnimeMachine scans the Torrent Pool read-only; AnimeMachine produces download plans, while qBittorrent or Ani-RSS performs the actual media download; external media libraries may participate in identification and playback and remain read-only by default. This layered state prevents an ambiguous match from being amplified through the automation chain into an incorrect directory or download.

Collector completeness filtering lives in `src/animemachine/torrents/collector_filter.py`, network discovery and the service loop in `collector.py`, and migration, retry, atomic persistence, and Pool auditing in `collector_state.py`. Collector reads the local Catalog without modifying it. SEARCH, FILTER, and metainfo-classifier versions are maintained separately: a FILTER change reevaluates already discovered results first, while only a SEARCH change creates a new historical-discovery generation. Torrent v1, v2, and hybrid identity is computed and stored centrally by `metainfo.py`, so separate entry points do not derive competing hash rules.

## Runtime boundaries

The main AnimeMachine implementation is under `src/animemachine`. Web, Catalog, Torrent indexing, library verification, qBittorrent, Ani-RSS, playback, and subtitle modules share SQLite evidence, but they do not share an assumption that every result is allowed to write everywhere. A result advances into directory mutation, download submission, or replacement only after it reaches the evidence strength required by the next stage.

AnimeMachine directly handles two classes of input: (1) local Torrent metadata placed in the shared directory by the user or optional Torrent Collector; and (2) records returned by external components connected by the user. Archive data, covers, and supplementary metadata pass through the shared network layer, which handles proxy/direct routing, mirror failover, retry backoff, response-size limits, and integrity validation. Proxy settings are reread per request, so changing system or environment proxy settings while AnimeMachine is running does not require a process restart; localhost, loopback, and private networks stay direct. Credentials are obtained only from environment variables, secret files, or local settings and are not written into public configuration or documentation.

Optional Torrent Collector runs as an independent Compose service. It has write access to the shared Torrent Pool, while AnimeMachine keeps the same Pool read-only. This boundary is intentional: Collector decides whether metadata may enter the candidate pool; AnimeMachine decides which work the candidate actually belongs to, whether it covers the target, and whether it should be downloaded. The former does not replace the latter.

## Data flow

```text
Bangumi Archive ──> Anime Catalog ──> titles, relationships, directory planning
Torrent Collector ─> shared Torrent Pool (collection metadata)
Torrent Pool ──────> incremental index ──> candidate policy and file manifests
Local library ─────> verification/history ──> differential completion and state
Ani-RSS ───────────> subscriptions and read-only media mapping
Candidate plan ────> qBittorrent / Ani-RSS ──> download-state feedback
```

Archive is built in background stages and published atomically only after integrity checks succeed. Large Torrent Pools and external media libraries are processed incrementally in short batches so one full scan does not hold a long database transaction. Web reads use independent connections and short transactions, allowing catalog construction, scanning, and interactive browsing to proceed concurrently.

Covers are another external dependency that can easily slow an entire application, so image network access is moved into the independent `ImageFetcher` process. AnimeMachine prefers Bangumi's roughly 400 px server-side scaled image, uses short-delay parallel fallback across the official source and available mirrors, and keeps the original-size image as the last fallback. Transient failures receive only short negative caching and that transient state is cleared on service restart. On a cache miss, the Web API returns a placeholder immediately and the front end replaces it after the background fetch succeeds. If “Reload” is pressed while the same work is already being fetched, one forced refresh is preserved instead of being silently swallowed by the in-flight request.

## Identity and directories

Directory handling follows one fixed order: **identify the work, identify the existing path, and only then consider creating a new path.** Identity is supported by titles, aliases, first-air month, series relationships, and existing files. If the existing library contains one uniquely credible nearby target, that path is reused. An incorrect directory is corrected in place before creating a second, “more standardized” directory. An official English subtitle is appended to a directory name only when it is semantically distinct from the original title and sufficiently supported by evidence.

The reason is that a logical Bangumi entry is not inherently equivalent to one physical directory. Split broadcasts, attached shorts, originally unsplit movie/OVA collections, and very long continuous works may require multiple logical members to share one physical target. Conversely, after physical targets are merged, Catalog logical members remain distinct. Every download plan therefore resolves to a canonical target before differential comparison with local files; it cannot skip the identity layer and build directories directly from a Torrent title.

## Resource policy

Torrents supplied by the user and Torrents gathered by Collector ultimately enter the same incremental index. Collector prefiltering only blocks metadata that clearly falls outside the automated-collection boundary; it does not replace AnimeMachine work identification, eligibility decisions, or ranking. A resource class is excluded only when the user explicitly disables it, and unrecognized values are grouped under “Other.” For a long-term archive this is safer than treating unknown values as errors, because older release naming is not always standardized.

Ranking uses a complete plan for one work or one series as its unit. AnimeMachine first determines whether the plan covers the expected episodes/volumes and then compares collection form, release group, source, resolution, subtitles, revision date, and related preferences. Resource quality therefore participates after coverage has been established; a higher resolution cannot compensate for missing episodes.

Only one AnimeMachine-managed task may exist for one infohash. A single collection can map different file indexes to multiple child works; when additional files are needed later, the existing task is reused and only previously unselected indexes are added. Every formal submission is preceded by an immutable plan. If the plan detects a target-directory conflict or a file scheduled for replacement, execution moves into staging, validation, or review rather than overwriting existing content directly.

## Work relationship graph algorithm

The work relationship graph first reconstructs relationship structure from many-to-many evidence and then determines how to draw it. The first stage processes the graph itself: (1) normalize Bangumi source relationships into directed edges; (2) add reverse relationships that can be established; (3) remove confirmed duplicate edges and provably transitive redundant edges; and (4) build strict series connected components. Only strong evidence permits AnimeMachine to repair an obvious mislink, add an entry into an isolated subseries, or re-anchor a relationship to the head of a subseries. Original source evidence is always retained; a corrected interpretation does not overwrite the source fact.

Layout happens in the second stage. Nodes are ordered by month, medium, and title sequence; works with the same medium or a clear numeric sequence are preferentially placed on the same row. Mainline sequels use the central straight track when possible, while side stories, derivatives, recaps, alternate adaptations, and cross-series relationships receive separate tracks and ports. The router evaluates node boundaries, occupied tracks, label rectangles, and bend cost together and can choose a straight segment, orthogonal polyline, or vertical detour when necessary. Long-span edges sharing one source are then layered by time and target position to reduce unreadable crossings. Complex graphs can hide selected relationship types, open full-screen, and export SVG/PNG.

## Repository structure

```text
src/                 runtime source and built-in static resources
config/              public configuration examples and JSON Schema
deploy/              local deployment, four Compose layouts, and shared Torrent Collector definition
packaging/            image-build definitions
scripts/              Windows, Linux, and macOS build/start/cleanup entry points
tests/                reproducible unit and integration tests
docs/                 user and development documentation
```

Runtime databases, Archive data, cover caches, logs, credentials, private paths, and build artifacts are runtime/local outputs and are not part of the source tree. This distinction also means source can be reacquired, while the state directory should be backed up; conversely, cleaning build caches must not touch the user Catalog or collection history.

## Database conventions

AnimeMachine uses two SQLite files to separate “large scanning writes” from “stable front-end queries.” (1) The Anime Catalog defaults to `$ANM_STATE_DIR/catalog/anime-catalog.sqlite3` and stores anime metadata derived from Bangumi Archive, work relationships, covers, external-media mappings, and runtime-state projections used by Web queries and plan generation. (2) The runtime database defaults to `$ANM_STATE_DIR/catalog/runtime.sqlite3` and stores the raw incremental Torrent-Pool index, verified identities, file partitions, submission evidence, and local-verification results. After a short runtime batch completes, only verified results are synchronized into the Catalog's `runtime_*` tables, so the front end does not compete directly with the scanning database.

Both database paths can be overridden through environment variables, but two AnimeMachine instances must not write to the same state directory concurrently. The Anime Catalog base structure is created by `src/animemachine/catalog/service.py`, runtime migrations are managed by `src/animemachine/catalog/migrations.py`, and query projections by `src/animemachine/torrents/runtime.py`. Migrations must be forward-compatible and repeatable. Credentials, private-path listings, and manual exports must not be written into public documentation or test fixtures.

### Anime Catalog

- `anime_work`: logical anime works, including Bangumi ID, original title, first-air month, medium, episode count, summary, and physical-merge role.
- `anime_title`: Chinese, English, Japanese titles and aliases; directory naming uses only verified original titles and genuinely necessary subtitles.
- `anime_staff`, `anime_cast`, `anime_studio`, `anime_studio_cluster`: normalized staff, cast, and studio data.
- `anime_tag`, `anime_theme`, `anime_theme_evidence`, `anime_country`: filter fields and their rule evidence.
- `anime_relation`: raw Archive relationships; `anime_relation_edge` and `anime_series_component` store normalized relationships and strict series connected components respectively.
- `anime_image`: validated cover MIME, bytes, source, and cache state; data is written atomically only after complete download and format validation.
- `external_library_source`, `external_media_file`: external read-only media-library sources, directories, and file-level evidence.
- `ani_rss_state`, `ani_rss_subscription`, `ani_rss_resource`, `ani_rss_action`: Ani-RSS synchronization state and idempotent operation records.
- `metadata_snapshot`, `metadata_evidence`, `metadata_repair_queue`: base-package provenance, supplementary metadata, and deferred repair tasks.

### Runtime database and query projections

- `torrent_source`, `torrent`, `torrent_manifest_file`: Torrent-Pool file fingerprints, infohashes, titles, sources, release groups, manifests, and parse errors; a source is reread only when its fingerprint changes.
- `anime_work`, `anime_work_member`, `torrent_work`: physical targets, logical members, and verified many-to-many Torrent-to-work mappings; `torrent_work` is authoritative evidence for identity and final path.
- `file_map`: canonical targets and selection evidence for each file index in a collection.
- `submission`, `submission_revision`, `submission_file_revision`: AnimeMachine-managed qBittorrent tasks and append-selection history; one infohash maps to one task.
- `asset_provenance`, `release_baseline`, `upgrade_candidate`: local-file provenance, comparison baselines, and verifiable upgrade candidates.
- `torrent_resolution`, `torrent_target_path`, `title_review`, `scope_exclusion`: scope, title, target, and handling reasons that cannot be resolved automatically.
- `runtime_work`, `runtime_torrent`, `runtime_torrent_work`, `runtime_torrent_file`, `runtime_file_map`, `runtime_submission`: front-end query projections in the Anime Catalog; they do not replace authoritative records in the runtime database.
- `download_plan`, `runtime_watch`, `runtime_watch_match`, `runtime_asset`, `runtime_completeness`, `runtime_review`: immutable plans, tracking, local completeness, and front-end review state stored in the Anime Catalog.

### State principles

AnimeMachine state handling can be reduced to six rules: (1) a logical work is not the same as a physical directory; split broadcasts, attached shorts, and originally unsplit collections may share one physical target; (2) unverified mappings, temporary proposals, and ambiguous matches cannot authorize directory changes or download submission; (3) local differential comparison is limited to the same canonical target, with fast mode skipping on canonical target plus exact byte size and exact mode hashing only when a comparison baseline exists; (4) replacement requires revision evidence, complete staging, and validation, and any failure preserves the old file; (5) magnet tasks are only registered until a complete manifest is available and do not automatically map files or submit downloads; and (6) configuration changes invalidate derived ranking and unexecuted plans only, not verified work identities or source evidence.

The common purpose of these rules is to keep state changes explainable. New evidence may change a conclusion, but a configuration edit or one ambiguous match must not erase facts that were already verified.

### Concurrency

Long imports and scans use short batch commits and avoid long-held transactions. Web reads use independent connections with WAL enabled. Cover network access runs entirely in a separate process and uses neither Web request threads nor the main process HTTP connection pool. Front-end progress comes from atomic state files or background-task tables. Time-consuming work remains isolated so its latency does not spread into ordinary browsing and queries.

[中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)  
[README](README.en.md) | [Deployment and Usage Guide](docs/guide.en.md) | [Architecture and Database](docs/architecture.en.md) | [Changelog](CHANGELOG.en.md)

# AnimeMachine · Automated Anime Library

AnimeMachine is a fully automated anime-library system. It organizes anime metadata, the Torrent pool, media directories, and external read-only media libraries under one local Catalog and state model. **AnimeMachine itself does not provide anime search or downloads**, nor does it bundle Torrents, magnet links, or media content. Users configure a local anime library or mount an external read-only library, map their own Torrent Pool or use Torrent Collector to gather collection-level `.torrent` metadata that matches the rules from public indexes, and connect components such as qBittorrent and Ani-RSS for actual downloads or subscriptions. As new works air and a collection grows, AnimeMachine maintains consistency across resource provenance, directory naming, media files, work metadata, series relationships, and existing holdings, supporting continuous management of very large anime libraries.

If the path from “resource discovery” to “stored in the library” is broken down into stages, AnimeMachine performs seven consecutive jobs: (1) material screening scans the Torrent pool, first deciding whether a release is complete and then comparing collection form, release group, source, resolution, and related criteria; (2) feeding sends confirmed tasks to qBittorrent or ongoing-series subscriptions to Ani-RSS; (3) primary processing plans multi-level directories from first-air month, official title, and series membership; (4) refinement establishes logical relationships such as prequels, sequels, recaps, side stories, derivatives, alternate adaptations, cross-series links, and character appearances; (5) quality inspection compares Torrent manifests with local files item by item, fills only what is missing, and stages/replaces old files only when reliable revision evidence exists; (6) packaging provides a Chinese, English, and Japanese Web interface for the catalog, candidates, library status, and series relationships; and (7) storage organizes downloaded, externally mapped, and pending works into a repository that can continue to grow without losing its structure.

![AnimeMachine library overview](docs/images/library-overview.png)

*Library overview: recent anime by default, with multiple filters and sorting methods.*

![AnimeMachine work detail](docs/images/work-detail.png)

*Work detail: principal metadata for one anime, read from Bangumi Archive.*

![AnimeMachine player handoff](docs/images/playback.png)

*Player handoff: AnimeMachine generates an M3U playlist for the complete work and passes it to a local player. Playback can start from a selected episode while keeping the full episode sequence, on any platform with a compatible player such as VLC or PotPlayer. An Ani-RSS API connection can also supply an HTTP-proxied playlist without a mounted Ani-RSS media directory, including byte-range seeking and short-interruption resume, and can receive new-series subscriptions.*

![AnimeMachine relationship graph](docs/images/relationship-graph.png)

*Relationship graph: generated from work-to-work evidence. The layout handles many-to-many and nested relationships by positioning nodes, separating edge tracks, correcting high-confidence source anomalies, and keeping the computation practical.*

## Main capabilities

- The anime base catalog is built from Bangumi Archive. On first startup, AnimeMachine builds a local SQLite Catalog in the background and publishes it atomically after integrity checks succeed.
- The Torrent pool is scanned incrementally. Files whose fingerprints have not changed are not parsed again; even during a large full scan, completed batches can already be queried and used for resource selection.
- Automated Docker layouts can enable Torrent Collector. It combines title, Torrent-manifest, and local-Catalog evidence into `accept / reject / defer`: only `accept` is written to the shared Torrent pool, while insufficient evidence remains `defer` instead of guessing completeness from a fixed episode span. Weekly episode releases can be subscribed through Ani-RSS and accessed remotely.
- Download planning supports complete collections, episode/volume combinations, differential completion, and adding file selections to an existing task with the same infohash. AnimeMachine-managed qBittorrent jobs are planned first and are always submitted stopped; the user starts them in qBittorrent after confirming the plan.
- The managed library can be a local media directory or a writable UNC/NAS directory. Existing media can also be mapped as an external read-only library without requiring migration or renaming.
- qBittorrent, Ani-RSS, and Torrent Collector are all optional. They can be used independently or combined into a complete automated collection chain under Compose.
- Version 0.2.1 gates a remote Ani-RSS source on the current credentials, endpoint, and effective proxy/direct route. A proxy or `NO_PROXY` change immediately makes the old `ready` snapshot temporarily unavailable for current resource/playback use and triggers compensating synchronization; successful revalidation restores it automatically. Torrent handling, Bangumi images, the local library, and independently mounted read-only media continue while Ani-RSS is unavailable, and local/LAN Ani-RSS remains direct. When a successful synchronization finds an explicit Ani-RSS cover for a work, it also releases any earlier `no_cover` negative cache left by AnimeMachine’s own image sources, allowing the next normal image request to use Ani-RSS without waiting for the cover-maintenance cadence. Changing the Ani-RSS endpoint or API key also invalidates the previous instance’s resource-search cache immediately; a late result from an in-flight search on the old source is discarded by provider-generation checks, and resource discovery becomes due immediately after the replacement source is revalidated. A remote resource search also captures its effective proxy-route generation at start; if the proxy or `NO_PROXY` changes while that search is in flight, its result is discarded before commit so a slow request cannot write across network generations.
- Ani-RSS “resource routing mode” controls resource discovery and planning only. Even in Manual mode, connected subscriptions and API-playable media still refresh on the sync interval. Localhost/LAN cover-failure cooldowns are isolated from unrelated proxy toggles so proxy churn cannot trigger premature local `/api/file` retries.
- Playback generates M3U playlists for the system player, VLC, or PotPlayer. If server and client paths differ, a separate player-accessible path mapping can be configured.
- For archival resources, AnimeMachine can inspect embedded/external subtitles and connect to user-configured subtitle services; subtitle handling does not grant write access to external read-only media.
- Relationship graphs show where each anime sits in its series and provide direct navigation to related works; node titles use the same current-interface-language rule as cards and detail pages. Version 0.2.1 rechecks stored English aliases when an existing Catalog is upgraded, rejects any alias containing non-Latin alphabetic script that was mislabeled as English (including CJK, Cyrillic, Greek, Arabic, and similar scripts), and repairs verifiable English display titles without rebuilding the Catalog. When upstream data genuinely has no English title, the English UI temporarily falls back to the work’s original title for usability without storing or labeling that fallback as English; a verified English title automatically takes priority once available.

## Quick start

### Local execution

Windows users run `scripts/windows/AnimeMachine.cmd`; Linux users run `scripts/unix/AnimeMachine-Linux.sh`; macOS users run `scripts/unix/AnimeMachine-macOS.command`. A first startup directly from source requires Python 3.11 or later; the launcher creates an isolated environment and installs the current project. No `.env.local` is required: the default listener is `0.0.0.0:8787`; use `http://127.0.0.1:8787` locally or `http://<host-IP>:8787` from another LAN device. A zero-configuration first start generates the administrator credentials and stores them in a restricted credential file; while that bootstrap administrator remains active, later starts print the same login credentials again so they are recoverable from the console.

See the [Deployment and Usage Guide](docs/guide.en.md#local-deployment) for details. Each Release keeps launchers for Windows, Linux, and macOS, but only the runtime or dependencies for the build platform are bundled directly. When running the package on another operating system, install Python 3.11 or later first; the initial launch may also retrieve compatible dependencies for that platform.

### Docker Compose

`deploy/compose` provides four predefined layouts: (1) standalone AnimeMachine; (2) AnimeMachine with external qBittorrent; (3) Torrent Collector with bundled qBittorrent; and (4) a full stack in which Torrent Collector, qBittorrent, and Ani-RSS are managed by one Compose project. The first three layouts may connect to external Ani-RSS, while catalog, directory, Torrent Pool, and local-library management remain available without it.

The 0.2.1 presets pin the public image `ghcr.io/kyupi-git/animemachine:0.2.1` by default. The full Compose layout also pins Ani-RSS `v3.2.28` and qBittorrent `5.2.3`, and the current adapters are verified against those interfaces; an external qBittorrent instance must remain 5.2.0 or later. Changing the image to `latest` makes Compose follow later releases; production deployments should keep specific version tags pinned.

Enter the selected directory and run `docker compose up -d` directly; `.env` is optional. Defaults publish `0.0.0.0:8787` and generate the AnimeMachine administrator password, while layouts 3/4 also generate and persist random API credentials for bundled qBittorrent/Ani-RSS, and AnimeMachine waits for the credential bootstrap to succeed and the managed service containers to start before its first application process begins, eliminating the first-start race in a zero-configuration deployment. `04-full-stack/compose.yaml` is a self-contained single-file deployment definition: copying only that YAML into an empty directory, with no `.env` beside it, is sufficient for a default first deployment using relative directories. Ani-RSS writes completed media to the shared `/Media` mount by default (host `./external/ani-rss`), while qBittorrent uses `/downloads/incomplete` only for incomplete data, so AnimeMachine can discover and play completed Ani-RSS downloads without additional path configuration. Copy `.env.example` to `.env` only to override host paths, credentials, PUID/PGID, proxy settings, or ports. See the [Deployment and Usage Guide](docs/guide.en.md#docker-compose) for the complete configuration.

## Documentation

- [Deployment and Usage Guide](docs/guide.en.md): local/NAS/Compose deployment, initial catalog creation, downloading, Ani-RSS, playback, and routine maintenance.
- [Architecture and Database](docs/architecture.en.md): module boundaries, data flows, directory rules, relationship-graph algorithms, SQLite entities, and state principles.
- [Third-party components and data boundaries](THIRD-PARTY.md); [Security policy](SECURITY.md); [Contributing](CONTRIBUTING.md).

## Build and cleanup

Windows:

```text
scripts\windows\Build-Release.cmd
scripts\windows\Build-Docker-Image.cmd
scripts\windows\Clean-AnimeMachine.cmd
```

Linux/macOS:

```bash
./scripts/unix/build-release.sh
./scripts/unix/build-docker-image.sh
./scripts/unix/Clean-AnimeMachine.sh
```

All build artifacts are written to `dist`. Cleanup scripts remove build, test, and interpreter caches while preserving databases, cover caches, user configuration, and library history. Their purpose is to remove development artifacts, not to reset AnimeMachine.

## Data and license

The anime base catalog comes from [Bangumi Archive](https://github.com/bangumi/Archive), and Ani-RSS integration follows [ani-rss](https://github.com/wushuo894/ani-rss). See [THIRD-PARTY.md](THIRD-PARTY.md) for third-party components, data sources, and license boundaries.

AnimeMachine is released under AGPL-3.0-only.

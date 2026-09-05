[中文](CHANGELOG.md) | [English](CHANGELOG.en.md) | [日本語](CHANGELOG.ja.md)  
[README](README.en.md) | [Deployment and Usage Guide](docs/guide.en.md) | [Architecture and Database](docs/architecture.en.md) | [Changelog](CHANGELOG.en.md)

# Changelog

This file records user-facing changes in reverse version order. Internal refactoring, test-only adjustments, and details that do not affect use are omitted.

## 0.2.1

0.2.1 focuses on delivery stability for Ani-RSS coordination, multilingual titles, and image recovery. It remains compatible with 0.2.0 configuration and Catalog data, so no Catalog rebuild or media-directory migration is required.

### Added and improved

- Improved multilingual titles: cards, filters, details, and relationship graphs follow the UI language; missing English titles display the original title without storing it as English.
- Improved Ani-RSS synchronization: subscriptions, playable media, resources, and covers fail independently; incomplete synchronization becomes eligible for a compensating retry within at most five minutes; API outages hide stale remote sources while the local library, Torrent flow, and independently mounted read-only media continue.
- Improved Ani-RSS network recovery: synchronization follows the effective proxy-route generation; proxy or `NO_PROXY` changes retire old snapshots until immediate revalidation succeeds.
- Improved Ani-RSS image coordination: remote covers are content-validated; endpoint failures quickly use AnimeMachine sources, and recovery clears stale `no_cover` state.
- Improved Ani-RSS resource discovery: recent works lead a rolling 24-month scan; failures retry soon, and unfinished passes make the next scan skip instead of run concurrently.
- Clarified Ani-RSS resource-routing modes: Manual stops background resource discovery and planning only; connected subscriptions and API-playable media still refresh on the sync interval and resume normally after recovery.
- Improved zero-configuration first deployment: local launchers and all four Compose layouts listen on `0.0.0.0:8787` by default; remotely reachable deployments enable authentication and persist/reprint automatically generated bootstrap administrator credentials; `.env` is optional overrides only; `04-full-stack/compose.yaml` embeds Collector and can be copied alone into an empty directory; managed qBittorrent/Ani-RSS secrets are generated once and reused, empty media mounts are prepared before dropping service privileges, and the full stack shares `/Media` for Ani-RSS final downloads/qBittorrent visibility/AnimeMachine playback while keeping incomplete data under `/downloads/incomplete`.

### Fixed

- Fixed malformed or truncated Ani-RSS responses being treated as empty results; invalid subscription, playlist, or resource payloads now retain the latest valid snapshot.
- Fixed late resource writes after Ani-RSS endpoint, API-key, or proxy-route changes; source-generation changes invalidate old caches and make discovery due again.
- Fixed localhost/LAN Ani-RSS cover cooldowns being cleared by unrelated proxy changes; direct local cooldowns now ignore proxy churn while remote endpoints still revalidate after an actual route change.
- Fixed clock rollback or abnormal future timestamps delaying image negative-cache recovery, resource rechecks, and external read-only media scans.
- Fixed Ani-RSS on-demand connection failures interrupting local candidate planning; AnimeMachine candidates remain usable when the optional service fails.
- Fixed local launchers still using loopback/port 8877 instead of the canonical 8787 endpoint, the legacy `serve/demo` entry point retaining loopback binding/the old 8765 default, and Compose layouts failing to start directly when `.env`, directory overrides, or pre-filled managed-service secrets were absent; managed Compose now also waits for credential bootstrap completion to remove the first-start race with empty configuration directories, and the zero-configuration Docker Ani-RSS state-sync default now matches the application/documented 30-minute default; the Windows launcher also probes an already-running instance through the effective bind address, including IPv6.
- Fixed subtitle search reading an obsolete Catalog field and raising SQL errors, and removed inactive MyAnimeList fallback claims from configuration and About.

## 0.2.0

0.2.0 completed the runtime integration of Ani-RSS and upgraded networking, background tasks, and application maintenance. Torrent, local media, and the existing image sources remain independent fallback paths; upgrades from older releases do not require rebuilding the Catalog or moving media directories.

### Added and improved

- Added unified Ani-RSS synchronization for subscriptions, playable media, episode counts, covers, and resource state, while retaining the latest valid snapshot when an individual part fails.
- Added Ani-RSS remote playback relay, allowing complete M3U generation without mounting its media directory, with byte ranges, seeking, and short-interruption resume.
- Added online application updates: portable builds support verification, health checks, and rollback, while Docker supports application-layer updates without mounting the Docker socket.
- Added system health and network diagnostics covering network, storage, images, playback, qBittorrent, and Ani-RSS, with health learning separated by network route.
- Added resource-region filters for China, Japan, Korea, the United States, Europe, and other regions.
- Improved image loading and resource warm-up with browse-priority batches and adaptive background cost based on foreground activity, system load, and network state.
- Improved source and library-state handling across Torrent, Ani-RSS, local, and external read-only media for filtering, display, playback, and health decisions.
- Improved credential management: Web-saved service credentials use permission-restricted storage and transactional configuration writes, while deployment secrets retain highest priority.
- Improved metadata for non-Japanese animation by preserving the actual original language and refining multilingual aliases, source-work relationships, and studio aggregation.

### Fixed

- Fixed incomplete Ani-RSS responses, individual media-refresh failures, or clock rollback causing subscription state and playable episode counts to stall or be cleared incorrectly.
- Fixed Ani-RSS disconnects, credential changes, or unavailable optional media directories leaving stale resources planned, playback waiting on upstream, or background threads failing.
- Fixed region permissions, source filters, and library state producing inconsistent results across cards, details, and different database call paths.
- Fixed configuration or credentials being partially written after validation failures, and connection tests prematurely saving or clearing newly entered credentials.
- Fixed cross-year winter seasons, future dates, multilingual titles, and abnormal year/month state being parsed incorrectly during filtering or recovery.
- Fixed playback queue cleanup deleting distinct episodes by file size, and resource identification issues caused by duplicate info-hashes, path case, or stale Torrent copies.
- Fixed Compose listen addresses, online-update health probes, and proxy configuration causing unreachable containers or incorrect update rollback.
- Fixed large-download source switching, reuse of damaged temporary files, offline image counting, and Linux/macOS update-package type validation.

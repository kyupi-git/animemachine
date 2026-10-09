[中文](CHANGELOG.md) | [English](CHANGELOG.en.md) | [日本語](CHANGELOG.ja.md)
[README](README.en.md) | [Setup and Usage Guide](docs/guide.en.md) | [Architecture](docs/architecture.en.md) | [Changelog](CHANGELOG.en.md)

# Changelog

Changes affecting everyday use, with the newest release first.

## 0.3.3

0.3.3 expands film release information in Release radar, fixes Archive version checks, application update checks, and season resource searches, and improves diagnostic loading. Existing settings, media, and cover caches carry over when you upgrade.

### Added and improved

- Improved Release radar: newer premieres appear first, with the latest episode update breaking same-day ties. Searches also check films that premiered within the past year; when no BD date is available, the earliest resource publication determines the season. BD and resource dates are labeled separately.
- Improved diagnostic loading: local runtime status appears first, with each result displayed independently and a message when a request times out.

### Fixed

- Fixed unnecessary Archive downloads and Catalog rebuilds by checking versions before downloading and skipping stale mirror manifests.
- Fixed update checks getting stuck when all sources are in cooldown; manual checks can retry immediately.
- Fixed identifier conflicts caused by duplicate resources and shared URLs; searches continue after an individual title fails and report the correct failure count.

## 0.3.2

0.3.2 improves Ani-RSS matching and synchronization, Release radar, and subscription feedback. Existing settings, media, and cover caches carry over when you upgrade.

### Added and improved

- Improved Release radar: show a whole season on one page, with title counts in the season tabs, Ani-RSS state synchronization, search progress, and retries for unfinished titles.
- Improved layout and feedback: unified action icons and more compact cards, details, and settings; subscriptions respond immediately, and scheduled update checks are enabled by default.
- Improved local library management: create placeholder folders for subscribed titles in one click, reuse existing folders, and resume after interruption; verification shows progress and assesses completeness against current files.

### Fixed

- Fixed missing resources for some sequels by reusing known release pages and checking title identifiers. Subscription refreshes now fetch media only for the affected title.
- Fixed overwritten cover candidates, enlarged thumbnails, and missing detail covers. Ani-RSS originals are reused without recompression, old thumbnail caches upgrade automatically, and Archive updates preserve existing images.

## 0.3.1

0.3.1 improves Release radar, subscription progress, and title cards, with updated Archive checks and reorganized multilingual documentation. Existing settings and collections carry over when you upgrade.

### Added and improved

- Improved Release radar: added premiere dates, season-wide resource searches, and column sorting across the whole season; latest episode counts prioritize Ani-RSS records.
- Improved title cards and subscriptions: combined status rows and added episode update times and counts; new subscriptions refresh progress and move to the front in “New Episode Follow-up” and “Random”, within the current filters.
- Improved Archive updates: check every Thursday at 02:12 UTC+8, with one retry 24 hours later if no new archive is found or the check fails, then resume the weekly schedule.
- Improved setup and usage guidance: reorganized all three documentation languages, added folder diagrams, and completed release attachments; initial Catalog builds show download, parsing, and database progress.

### Fixed

- Fixed card flickering during background refresh and dialog interactions, and display issues with mobile relationship graphs and popovers.
- Fixed season episode numbers, resource dates and sizes, and subtitle playback for external read-only media.

## 0.3.0

0.3.0 adds Release radar and improves episode progress, work details, and memory use. Existing Catalogs, media directories, and 0.2.x configuration remain compatible; required fields migrate automatically without rebuilding the Catalog or moving media.

### Added and improved

- Added Release radar with paginated previous/current/next seasons and automatic rollover seven days before each quarter; it shows localized titles, media type, latest/total episodes, subscription state, and update time, with unread highlights and direct subscription.
- Improved release discovery: films can appear in the season of their theatrical, streaming, or BD/Blu-ray release; serials use confirmed episode advances, including updates found by resource scans for unsubscribed works.
- Improved episode progress: details and radar share work-local numbering, convert cumulative series numbers when supported by clear evidence, and prioritize the known total for the current work.
- Improved work details: library inventory and playback sources are separate, duplicate media entries are reduced, subtitle search has one entry point, and Ani-RSS subscriptions remain manageable before media arrives or while paused.
- Improved defaults: all three availability options are selected initially and after reset, including compatibility with the legacy `available` default; M3U playback stays enabled without a redundant master switch.
- Reduced memory during initial Catalog builds and periodic scans through compact Archive data, lazy title indexes, bounded parsing caches, streamed Torrent rows, and reliable connection cleanup on errors.

### Fixed

- Fixed false episode announcements after redownloading, episode-counter rollback and recovery, or download-clock rollback; stale follow-up evidence is disabled during disconnection and restored after reconnection.
- Fixed progress leaking between works sharing a physical directory, and stale episode, download-time, or media evidence after subscription deletion or remapping.
- Fixed required user-creation fields blocking unrelated settings, failed saves changing runtime configuration early, and additional media libraries being lost on save.
- Fixed stale responses overriding newer detail or playback-source selections, and recent radar read history being evicted incorrectly when its limit is reached.
- Fixed expanded title search bypassing radar filters or region permissions and malformed release dates being accepted; strengthened publication checks to exclude configuration backups.

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

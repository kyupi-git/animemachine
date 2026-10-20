[中文](CONTRIBUTING.md) | [English](CONTRIBUTING.en.md) | [日本語](CONTRIBUTING.ja.md)

# Contributing

AnimeMachine uses Python 3.11 or newer, plain HTML, CSS and JavaScript, and SQLite for title metadata and runtime state.

## Component roles

| Component | Purpose |
| --- | --- |
| AnimeMachine | Browse titles, match media, plan downloads, and manage viewing records and playlists |
| Bangumi Archive | Supply titles, dates, credits and title relationships |
| qBittorrent | Receive download jobs and save media; connect an existing instance or deploy it with the app |
| Ani-RSS | Discover releases, manage subscriptions, and provide media and cached covers; optional |
| Torrent Collector | Collect Torrent metadata for title searches and download plans |
| PotPlayer extension | Report precise playback positions from the player; playback also works without it |

Components communicate through APIs or shared directories. See the [deployment guide](docs/guide.en.md) for setup and the [architecture](docs/architecture.en.md) for data flow and boundaries.

## Local development

Create a virtual environment in the project directory:

```sh
python -m venv .venv
```

Activate it, then install the development dependencies and run the checks:

```sh
python -m pip install -e ".[test]"
python scripts/test_all.py
python scripts/check_quality.py
```

Use Node.js to check JavaScript syntax. For deployment changes, install Docker Compose and validate the templates:

```text
node --check src/animemachine/web/static/app.js
python scripts/check_compose.py
```

## Repository layout

| Directory | Contents |
| --- | --- |
| `src/animemachine` | Application code, interface and bundled resources |
| `tests` | Synthetic fixtures, unit tests and integration tests |
| `packaging` | Docker image and release definitions |
| `deploy/compose` | Default full stack, alternative templates and standalone collector configuration |
| `scripts` | Tests, validation, builds and maintenance tools |
| `docs` | Guides, architecture and configuration references in three languages |

## Submitting changes

Keep each change focused on a concrete problem and describe the resulting behavior and validation. Use reproducible examples for title matching, relationships and metadata imports. Changes to stored data include a migration and upgrade tests. Maintain Chinese, English and Japanese interfaces and user documentation together.

Automated tests use synthetic metadata and isolated temporary directories; download and player integration tests use a dedicated environment. Contributions contain source, maintained documentation and reproducible examples. Instance settings, credentials, media and build outputs stay local.

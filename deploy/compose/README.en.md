[中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)

# Docker Compose setup guide

**Choose exactly one `compose.yaml` and start it once.** Numbers 01–04 are alternative setups, not four steps to run in sequence.

## 1. Install Docker

- **Windows / macOS:** install and start [Docker Desktop](https://docs.docker.com/get-started/get-docker/).
- **Linux / NAS:** install and start Docker Engine and the Docker Compose plugin. Your NAS may offer a Container Manager application. Follow [Docker's installation guide](https://docs.docker.com/compose/install/).
- Requirements: Docker Engine 24+ and Docker Compose 2.20.3+. Check both in a terminal:

```sh
docker version
docker compose version
```

If Docker says it cannot connect to the daemon, start its service. If `compose` is not recognized, install the Compose plugin. The commands below use `docker compose` with a space.

## 2. Choose one setup

| Your situation | Download | Services |
| --- | --- | --- |
| Complete setup (recommended) | [Default full stack](compose.yaml), same as [04](templates/04-full-stack/compose.yaml) | AnimeMachine, qBittorrent, Ani-RSS, Torrent Collector |
| Only want to browse and play existing media | [01 · Standalone](templates/01-animemachine-standalone/compose.yaml) | AnimeMachine |
| Already have a configured qBittorrent | [02 · Existing downloader](templates/02-animemachine-external-qbt/compose.yaml) | AnimeMachine, connected to your qBittorrent |
| Want a downloader but no managed Ani-RSS | [03 · Managed downloader](templates/03-animemachine-managed-qbt/compose.yaml) | AnimeMachine, qBittorrent, Torrent Collector |

Torrent Collector collects `.torrent` metadata; it does not download videos. You can connect an existing Ani-RSS later with 01/02. [advanced/collector](advanced/collector/compose.yaml) runs the collector separately.

## 3. Save the file and start

1. Create a permanent folder, for example `AnimeMachine`, on a disk where you want to keep your configuration and data. **Keep using this same folder for future updates.**
2. Open the chosen `compose.yaml` above. On GitHub, click **Download raw file** and save it inside that folder as exactly `compose.yaml` (not `compose.yaml.txt`). One YAML file is sufficient; you **do not need to copy `.env.example`, enter API keys or run initialization scripts** for the defaults.
3. Open a terminal in this folder. On Windows, right-click an empty area of the folder and choose **Open in Terminal**. On Linux / macOS, open a terminal there or use `cd` to navigate to it. A NAS's Compose-project screen can import the same YAML with this folder as its project directory.
4. Run these commands in the folder:

```sh
docker compose config --quiet
docker compose up -d
docker compose ps
```

The first command validates the file, the second downloads images and starts containers, and the third shows status. Initial image downloads can take time. The full stack normally has four services; 01 has one. If image downloads fail, check Docker network access, DNS and registry connectivity.

## 4. Log in for the first time

On the host computer, open `http://localhost:8787`; from another device, replace `localhost` with the LAN address of the NAS or host, for example `http://192.168.1.20:8787` (use your actual address).

AnimeMachine creates the initial administrator (default user `admin`) and a random password. Run the following and look for `AnimeMachine login`, `Username` and `Password` in the **initial startup** messages:

```sh
docker compose logs animemachine
```

The random password is **printed only when first generated**, not on subsequent restarts. If the original log is no longer available, the generated credentials are saved at `data/state/auth/initial-admin.txt` under the project folder (inside the container: `/Data/state/auth/initial-admin.txt`). If you have changed the password, use the new one. Updates and restarts preserve existing accounts.

Startup prepares seasonal titles first, so you can browse, subscribe and play while the remaining catalog is built in the background. Collection-directory creation and local Torrent downloads become available when the full catalog is ready. open Settings → Connections and check `/Library` for the writable collection and `/External` for optionally mapped read-only media. Managed Ani-RSS uses `/Media`. These are **container paths**, not host filesystem paths.

## 5. Where is data stored?

The default Compose project creates these directories next to `compose.yaml`:

| Folder | Purpose |
| --- | --- |
| `config` | Application and managed-service configuration, credentials |
| `data` | Catalog state, accounts, and runtime data |
| `library` | Writable AnimeMachine media collection |
| `imports` / `torrents` | Optional imports / `.torrent` pool (read-only to AnimeMachine) |
| `external/read-only` | Existing media mapped read-only |
| `external/ani-rss` | Completed media shared by managed Ani-RSS and downloader |

Map existing videos through `external/read-only`, and keep the writable collection in `library`. To store the library elsewhere, copy the chosen template's `.env.example` to `.env` alongside `compose.yaml`, change `ANM_LIBRARY_DIR=./library` to the **host** path (for example `/srv/anime` on Linux), and run `docker compose up -d` again. Ensure the folder exists and is writable by the configured user/group IDs (`PUID`/`PGID`, default 1000). AnimeMachine continues to see `/Library` inside its container.

For setup 02, make sure the existing qBittorrent Web API is reachable from the container. Then set `ANM_MANAGED_QBITTORRENT_URL` and map the downloader's **container media path** with `ANM_QBT_LIBRARY_DIR` in `.env`. Confirm both the host and container mappings.

## 6. Operations, updates and backups

Run commands from the **original Compose folder**:

```sh
docker compose ps
docker compose logs --tail 100 animemachine
docker compose pull
docker compose up -d
```

The first two show status and recent logs; the latter two pull and recreate images. Default images are pinned to `0.5.2`. To upgrade to a newer release, first update the image tags in `compose.yaml` (or any `ANM_IMAGE` overrides in `.env`). Pulling alone does not upgrade a pinned version.

For backups, run `docker compose down` to stop services and preserve data. Back up `compose.yaml`, optional `.env`, `config`, `data`, `library`, actual media/Torrent directories, and the collector's named volume when used. Run `docker compose up -d` afterward. Give each AnimeMachine instance its own `data` directory. When changing older deployments, `docker compose up -d --remove-orphans` removes obsolete services, so check that none are custom services you want to keep.

## 7. Troubleshooting

- **Port 8787 does not open:** run `docker compose ps` and `docker compose logs --tail 100 animemachine`. Check the NAS address, firewall and port conflicts. Set `ANM_WEB_PORT=8788` in `.env`, run `docker compose up -d`, then use `:8788` if needed.
- **`permission denied`:** check mount permissions and `PUID`/`PGID`; on Linux, your terminal user must also have Docker access. Grant the required permissions to the user running the containers.
- **qBittorrent / Ani-RSS interface not reachable over LAN:** the default addresses are `http://NAS-address:8080` and `http://NAS-address:7789`. Generated passwords are saved in `config/qbittorrent/.animemachine/web-password` and `config/ani-rss/.animemachine/web-password`; upgrades keep existing passwords. See the [service access reference](../../docs/reference.en.md#service-access).
- **Player cannot open media from another computer:** configure the service URL that the player device can reach. See [connections and path mappings](../../docs/guide.en.md#connections).
- **Containers removed by mistake:** the data can be restored by running `docker compose up -d` in the same folder if mounts and named volumes remain. Deleting directories or using `down -v` can delete data.

For other features, see the [setup and usage guide](../../docs/guide.en.md).

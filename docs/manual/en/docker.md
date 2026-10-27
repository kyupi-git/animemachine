# 2. Linux and Docker

The default stack runs four services: AnimeMachine for browsing and controls, Ani-RSS for subscriptions, qBittorrent for downloads, and Torrent Collector for Torrent searches. Connections and accounts are initialized automatically.

## Prepare a project folder

1. Install [Docker Engine](https://docs.docker.com/engine/install/) and the [Compose plugin](https://docs.docker.com/compose/install/) for your Linux distribution. Use Docker Engine 24+ and Compose 2.20.3+.
2. Open a terminal on the computer or server that will run AnimeMachine.
3. Create a permanent project folder on a disk with enough space for your media:

```sh
mkdir -p ~/animemachine
cd ~/animemachine
```

The stack creates `config`, `data`, `library`, `torrents`, and `external` folders in this project. Media goes into `library` or `external/ani-rss`, so choose a disk with enough space.

## Download the Compose file

[Download compose.yaml](https://github.com/kyupi-git/animemachine/releases/latest/download/compose.yaml) and save it in the project folder. The release package also includes the same file at its root. Keep the complete file name **`compose.yaml`**. You can also download it in the terminal:

```sh
curl -fL https://raw.githubusercontent.com/kyupi-git/animemachine/main/deploy/compose/compose.yaml -o compose.yaml
```

Use one Compose file for the whole stack. No separate component accounts or configuration files are needed for the first start.

## Use a terminal

In the folder containing `compose.yaml`, run:

```sh
docker compose up -d
```

Check the services:

```sh
docker compose ps
```

**Expected result:** all four services are running. Ani-RSS and the collector wait for AnimeMachine to become ready. The first command shows image download progress before starting the containers.

## Open the page and find your password

1. Run the command below to find the initial account and random password.
2. On the same computer, open **<http://localhost:8787>**. From another device, use the server's LAN address, such as `http://192.168.1.20:8787`.
3. Sign in and continue to [First login](first-use.md).

Read the startup log:

```sh
docker compose logs animemachine
```

| Page | LAN address |
| --- | --- |
| AnimeMachine | `http://NAS-address:8787` |
| Ani-RSS | `http://NAS-address:7789` |
| qBittorrent | `http://NAS-address:8080` |

Most operations are available in AnimeMachine. To open the other management pages, use `admin`; their initial passwords are in `config/ani-rss/.animemachine/web-password` and `config/qbittorrent/.animemachine/web-password` under the project folder.

## Existing services and other disks

If you already run Ani-RSS or qBittorrent, use [preset 01 or 02](https://github.com/kyupi-git/animemachine/tree/main/deploy/compose/templates), then connect your service in **Settings → Connections**. The default file and the presets are alternative deployments; choose one.

For another media disk, follow [folder mapping](collections.md#docker-folders). The full stack checks public source connectivity and selects an available alternate source when needed.

## Common deployment questions

- **Configuration file not found:** run the command in the folder containing `compose.yaml`; check that it is not named `compose.yaml.txt`.
- **Image download failed:** check the Docker error and retry the same command after restoring connectivity. Existing downloaded layers are reused.
- **Docker permission denied:** use an account with Docker access; if your installation requires it, run the Docker commands with `sudo`.
- **Port is in use:** put an unused port such as `ANM_WEB_PORT=8788` in a `.env` file alongside Compose, run `docker compose up -d`, and use port 8788 in the browser.
- **Stopping temporarily:** run `docker compose stop`. Settings, records, and media stay in the project folders.

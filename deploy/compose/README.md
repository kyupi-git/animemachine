[中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)

# Docker Compose 部署指南

**只选择一份 `compose.yaml`，执行一次启动命令**；编号 01–04 表示不同的部署组合，不是先后执行的四个步骤。

## 1. 准备 Docker

- **Windows / macOS**：安装并启动 [Docker Desktop](https://docs.docker.com/get-started/get-docker/)，确认 Docker Engine 正在运行。
- **Linux / NAS**：安装、启动 Docker Engine 和 Docker Compose 插件；NAS 可使用系统的“容器管理”应用。操作步骤见 [Docker 官方安装文档](https://docs.docker.com/compose/install/)。
- 本项目要求 Docker Engine 24+、Docker Compose 2.20.3+。在终端执行以下两条命令，均能显示版本后再继续：

```sh
docker version
docker compose version
```

若看到“Cannot connect to the Docker daemon”，请先启动 Docker 服务；若提示 `compose` 不是有效命令，请安装 Compose 插件。下文统一使用 `docker compose`（中间有空格）。

## 2. 只选择一种部署方案

| 场景 | 配置文件 | 包含的服务 |
| --- | --- | --- |
| 完整功能（推荐） | [默认完整组合](compose.yaml)，与 [04](templates/04-full-stack/compose.yaml) 相同 | AnimeMachine、qBittorrent、Ani-RSS、Torrent Collector |
| 只想浏览作品与播放现有媒体 | [01 · 单独运行](templates/01-animemachine-standalone/compose.yaml) | AnimeMachine |
| 已经安装并配置好 qBittorrent | [02 · 连接现有下载器](templates/02-animemachine-external-qbt/compose.yaml) | AnimeMachine；连接现有 qBittorrent |
| 需要下载器，但暂不使用 Ani-RSS | [03 · 内置下载器](templates/03-animemachine-managed-qbt/compose.yaml) | AnimeMachine、qBittorrent、Torrent Collector |

`Torrent Collector` 用于收集 `.torrent` 元数据，并不直接下载视频。01/02 也能以后再连接已有 Ani-RSS。需要**独立运行采集器**时可使用 [advanced/collector](advanced/collector/compose.yaml)。

## 3. 下载并启动

1. 在准备长期保存应用的磁盘上新建文件夹，例如 `AnimeMachine`。**以后更新仍使用此文件夹**。
2. 打开上表选定的 `compose.yaml` 链接。在 GitHub 点击 **Download raw file（下载原始文件）**，保存至 `AnimeMachine` 文件夹，文件名必须是 `compose.yaml`，不是 `compose.yaml.txt`。只需这一个 YAML 文件；首次使用**不需要复制 `.env.example`、填写 API Key 或运行初始化脚本**。
3. 在该文件夹打开终端。Windows 可右键单击文件夹空白处，选择“在终端中打开”；macOS / Linux 可在文件夹中打开终端，或者使用 `cd` 切换到该文件夹。NAS 的“Compose 项目”功能也可以导入该 YAML 文件，选择此文件夹作为项目目录。
4. 在这个文件夹中依次执行：

```sh
docker compose config --quiet
docker compose up -d
docker compose ps
```

`config --quiet` 检查配置；`up -d` 拉取镜像并在后台启动；`ps` 查看容器状态。首次下载镜像可能需要较长时间。完整组合通常有四个服务，01 只有一个。若无法拉取镜像，请先检查 NAS 的联网、DNS、镜像仓库访问及 Docker 配置。

## 4. 首次登录

在部署电脑的浏览器打开 `http://localhost:8787`；用另一台设备打开时改为 `http://NAS的局域网IP:8787`，例如 `http://192.168.1.20:8787`。替换示例 IP 为自己的实际地址。

首次启动会自动创建管理员（默认用户名 `admin`）和随机密码。通过以下命令查看**首次启动时**的日志，并寻找 `AnimeMachine login`、`Username` 和 `Password`：

```sh
docker compose logs animemachine
```

**随机密码仅在首次生成时打印**。若错过首次日志，初始凭据保存在项目文件夹的 `data/state/auth/initial-admin.txt`（或容器内 `/Data/state/auth/initial-admin.txt`）。修改过密码后，使用修改后的密码；更新和重启会保留现有账号。

首次启动优先准备当季作品，列表出现后即可浏览、订阅和播放，其余资料在后台补全。建立收藏目录和本地 Torrent 下载会在完整资料就绪后开放。在“设置 → 连接”核对收藏库 `/Library`，如需导入原有媒体再设置只读路径 `/External`；有 Ani-RSS 的组合会共享 `/Media`。容器内使用这些路径，不填写 Windows 或 NAS 的宿主机路径。

## 5. 文件保存到哪里？

默认情况下，下列目录会自动建立在 `compose.yaml` 旁边：

| 文件夹 | 用途 |
| --- | --- |
| `config` | 应用及受管服务配置、访问凭据 |
| `data` | 状态数据库、登录信息和应用运行数据 |
| `library` | AnimeMachine 可读写的动画收藏库 |
| `imports` / `torrents` | 可选导入目录 / `.torrent` 池；应用对池为只读 |
| `external/read-only` | 已有影片的只读映射入口 |
| `external/ani-rss` | 内置 Ani-RSS 与下载器共享的完成媒体目录 |

已有影片可映射到 `external/read-only`；可写收藏放在 `library`。要改为其他硬盘存放收藏，可将相应模板同目录的 `.env.example` 复制为 `.env`，把其中的 `ANM_LIBRARY_DIR=./library` 改成实际宿主机路径（如 Linux `/srv/anime`），保存后再次执行 `docker compose up -d`。请先保证目录存在、权限正确；默认写入用户/组 ID 均为 1000（`PUID`/`PGID`），请按 NAS 实际用户修改。设置中的容器路径仍是 `/Library`。

对于 02，先确认现有 qBittorrent 的 Web API 可从容器访问，再在 `.env` 设置 `ANM_MANAGED_QBITTORRENT_URL`，并按现有下载器的**容器路径**配置 `ANM_QBT_LIBRARY_DIR`；二者与宿主机路径不同，需要按实际挂载关系核对。

## 6. 日常管理、更新与备份

所有命令都要在保存 `compose.yaml` 的原文件夹内执行：

```sh
docker compose ps
docker compose logs --tail 100 animemachine
docker compose pull
docker compose up -d
```

前两条分别检查运行状态及最近日志；后两条用于更新镜像和重新建立容器。镜像默认固定版本 `0.5.0`，升级到后续版本前须先更新 `compose.yaml` 中的镜像标签（或 `.env` 里的 `ANM_IMAGE` 等覆盖值）。仅重复执行 `pull` 不会自动变更固定版本号。

维护前可执行 `docker compose down` 停止容器并保留数据。备份 `compose.yaml`、`.env`（若有）、`config`、`data`、`library` 和实际使用的媒体、Torrent 目录；完整组合中的采集器命名卷也应单独备份。备份完成后运行 `docker compose up -d`。多实例应各自使用独立的 `data` 文件夹。更新旧项目配置时，可以执行 `docker compose up -d --remove-orphans` 清理不再使用的旧服务，先确认没有需要保留的自定义服务。

## 7. 常见问题

- **访问不到 8787**：先运行 `docker compose ps` 和 `docker compose logs --tail 100 animemachine`；检查本机防火墙、NAS IP 及端口是否被其他应用占用。可在 `.env` 设 `ANM_WEB_PORT=8788` 并重新运行 `docker compose up -d`，访问地址相应改为 `:8788`。
- **`permission denied`**：检查所映射目录的读写权限及 `.env` 里的 `PUID`、`PGID`；Linux 终端还需要当前用户具备 Docker 操作权限。按运行容器的用户授予所需权限。
- **下载器或 Ani-RSS 的网页打不开**：默认可从局域网访问 `http://NAS地址:8080` 和 `http://NAS地址:7789`。首次生成的密码分别保存在 `config/qbittorrent/.animemachine/web-password` 和 `config/ani-rss/.animemachine/web-password`；升级沿用原密码。访问设置见[连接和管理端口说明](../../docs/reference.md#service-access)。
- **播放器无法读取 NAS 上的媒体**：浏览器与播放器可能位于另一台设备，需要在设置中填写可从播放器访问的服务地址；媒体直接路径映射另见[部署与使用指南](../../docs/guide.md#connections)。
- **误删容器后数据还在吗？** 只要原来的挂载文件夹和命名卷仍在，在同一文件夹运行 `docker compose up -d` 可重新创建容器。清除文件夹或执行 `down -v` 则可能丢失数据。

需要更详细的操作和功能解释，可继续阅读[部署与使用指南](../../docs/guide.md)。

[中文](guide.md) | [English](guide.en.md) | [日本語](guide.ja.md)  
[README](../README.md) | [部署与使用指南](guide.md) | [架构与数据库](architecture.md)

# AnimeMachine 部署指南

部署 AnimeMachine 时，首先确定两项内容：(1)AnimeMachine 管理的媒体存放位置；(2)现有 qBittorrent、Ani-RSS、Torrent 池和旧媒体库采用外置方式，或统一交给 Compose 管理。其它设置基本由这两项选择派生。

首次运行前，至少准备一个动画收藏库即可。Torrent 池和外部只读媒体库可以先为空，qBittorrent 与 Ani-RSS 也都不是启动 AnimeMachine 的前置条件；以后补接组件，不需要重新建库。

## 路径的含义

| 名称 | 权限 | 用途 | 推荐设置 |
|---|---|---|---|
| 动画收藏库 | 读写 | 保存 AnimeMachine 管理的媒体、占位、字幕和目录结构 | 本机磁盘、NAS 共享目录或容器 `/Library` |
| Torrent 池 | AnimeMachine 只读 | 用户或可选 Torrent Collector 放入 `.torrent` 的目录；允许包含子目录和无关文件 | 独立目录或容器 `/Torrents` |
| 外部只读媒体库 | 只读 | 映射已经存在的媒体，不移动、不改名、不删除 | ani-rss 或其它媒体目录；容器 `/External` |
| Ani-RSS 媒体目录 | 只读 | 将 Ani-RSS 已下载内容映射到作品页和播放列表 | 容器 `/Media` |
| 状态目录 | 读写 | 保存 SQLite、Archive、封面、字幕、计划和操作历史 | 本地 SSD；容器 `/Data` |
| 配置目录 | 读写 | 保存 `config.json`、证书和组件配置 | 容器 `/Config` |

这几类路径最容易混淆的是“宿主机路径”“AnimeMachine 看到的路径”和“qBittorrent 看到的路径”。本地运行时三者往往相同；Docker 或跨主机部署时则可能完全不同。AnimeMachine 只要求最终的路径映射关系真实可访问，并不要求不同进程使用同一串路径文字。

Windows 本地部署可以直接填写盘符或 UNC，例如 `D:\Anime`、`\\nas\anime`。访问 UNC 的是运行 AnimeMachine 的 Windows 账号，因此该账号必须能够直接读写共享目录；映射盘符属于当前登录会话，不适合作为长期后台服务的唯一依赖。建议使用同一账号实际新建、改名并删除一个测试文件，以确认权限完整。

Docker 中的 `/Library`、`/Torrents` 等是容器内部固定路径，用户只需要把宿主机真实目录挂载到这些位置。外部 qBittorrent 还要另外填写“qBittorrent 自己看到的收藏库路径”，它可能是 `/Anime`、`/downloads/anime` 或 UNC，不要求与 AnimeMachine 的 `/Library` 相同。Windows Docker Desktop 通常不适合直接把 UNC 当作 bind source；更稳妥的做法是先由宿主系统挂载 SMB/NFS，再把本地挂载点交给 Docker。Linux、macOS 和 fnOS 也建议采用同一思路。

![连接与路径设置](images/settings-connections.png)

*“设置 → 连接”：集中确认收藏库、Torrent Pool、外部媒体库、qBittorrent 与 Ani-RSS 的连接和路径状态。*

配置与 Ani-RSS 的连接时，可以在 Ani-RSS 设置界面的“登录设置”找到所需的 API key。

![Ani-RSS API key](images/anirss-apikey.png)

## 本地部署

### Windows 10/11

Windows 本地运行建议按以下顺序完成：(1)源码模式先安装 Python 3.11 或更高版本；官方 Windows Release 固定随包提供 Python 3.14 运行时，因此不要求额外安装 Python，根目录 `BUILD-INFO.json` 可核对版本、提交和构建环境；(2)源码模式运行 `scripts\windows\AnimeMachine.cmd`，Release 解压后运行根目录的 `AnimeMachine.cmd`；(3)首次启动会创建本地环境文件和初始管理员凭据，控制台会显示初始登录信息及其保存位置；凭据文件会继续保留，但以后启动不会反复生成新密码；(4)打开 `http://127.0.0.1:8877`，进入“设置 → 连接”，填写收藏库、Torrent 池和需要接入的外部组件；(5)源码模式的状态默认位于 `.local/state`，Release 默认位于 `data/state`，重复启动会继续使用原来的 Catalog、缓存和历史。

如果希望第一次启动前就准备好路径，可以把 `deploy/local/.env.local.example` 复制为 `.local/.env.local`，再取消需要项目的注释。Release 则使用根目录的 `.env.local`。不建议为了“省一步”把 NAS 用户名和密码长期写进 AnimeMachine 配置；Windows 本地运行应优先让运行账号本身取得共享目录权限。

### Linux/macOS

Linux 源码模式运行 `scripts/unix/AnimeMachine-Linux.sh`，macOS 运行 `scripts/unix/AnimeMachine-macOS.command`；首次启动会在 `.local/venv` 建立隔离环境，同样要求 Python 3.11 或更高版本。Release 解压后使用根目录中的对应启动脚本。每个 Release 都保留 Windows、Linux 和 macOS 入口，但跨构建平台运行时需要系统自己提供 Python 3.11+，首次启动也可能联网安装当前平台可用的依赖。

若解压工具丢失了 Unix 可执行位，可以先执行 `chmod +x AnimeMachine.sh AnimeMachine-Linux.sh AnimeMachine-macOS.command`。macOS 访问 NAS 时，建议先在 Finder 挂载 SMB，再使用 `/Volumes/共享名/...`；Linux 则更适合由系统在 `/mnt` 或 `/srv` 挂载 SMB/NFS。这样做的好处是 AnimeMachine 只处理普通文件路径，认证、断线重连和网络文件系统行为仍由操作系统负责。

## Docker Compose

Docker 部署要求 Docker Engine 24+ 和 Docker Compose 2.20.3+；如果 AnimeMachine 直接管理 qBittorrent，目标 qBittorrent 需要 5.2.0 或更新版本。进入所选方案目录，把 `.env.example` 复制为 `.env`，至少确认路径、AnimeMachine 管理员密码和外部服务密钥。使用内置 qBittorrent 或 Ani-RSS 时，可以先执行仓库中的 `Initialize-AnimeMachine` 脚本，由脚本生成 AnimeMachine、qBittorrent 和 Ani-RSS 所需凭据，并集中显示一次。

配置完成后运行：

```bash
docker compose up -d
docker compose logs -f animemachine
```

通常将 `.env` 与 `compose.yaml` 放在同一目录最为稳妥。如果自动化平台必须使用其它位置的环境文件，并通过 `--env-file` 启动 Compose，还需要把 `ANM_ENV_FILE` 指向同一文件的绝对路径；否则 Compose 与服务容器可能读取不同配置，造成变量值与服务行为不一致。

以 fnOS 为例，在通过 Docker Compose 方式部署后，首次启动时需要下载 Bangumi Archive 底包，请耐心等待。当日志出现 [sync] Complete 则说明底包解析完成，此时可以访问，作品图片仍在预后台加载中；当日志出现 [images] preload complete 则说明最近 6 个月的动画新作的图片已预加载完成。

![AnimeMachine 初始化日志](images/fnos-ready.png)

### 四种方案

| 目录 | 组件 | Torrent Pool 来源 | 推荐使用场景 |
|---|---|---|---|
| `01-animemachine-standalone` | AnimeMachine；可选外部 Ani-RSS | 用户维护目录或留空 | 独立维护动画底库、目录、外部媒体与 Torrent Pool；不配置 Ani-RSS 也可直接使用 |
| `02-animemachine-external-qbt` | AnimeMachine + 外部 qBittorrent；可选外部 Ani-RSS | 用户维护目录 | 手动收藏库；匹配 Torrent Pool 资源并提交到外部 qBittorrent |
| `03-animemachine-managed-qbt` | AnimeMachine + Torrent Collector + 内置 qBittorrent；可选外部 Ani-RSS | Collector 自动收集资源 | 自动收藏库；Collector 收集资源，内置 qBittorrent 维护收藏库 |
| `04-full-stack` | AnimeMachine + Torrent Collector + 内置 qBittorrent + 内置 Ani-RSS | Collector 自动收集合集 | 全自动收藏和订阅；所有组件由同一 Compose 项目管理 |

四种方案没有“低配到高配”的继承关系，都可以独立使用。前三种方案的 Ani-RSS 连接均为可选；已经有稳定 qBittorrent 的机器通常更适合 02，希望 AnimeMachine 连同下载链路一起迁移到 NAS，再考虑 03 或 04。

03、04 会引用 `deploy/compose/torrent-collector.yaml` 中的共享服务定义，发布包会把该文件与方案的 `compose.yaml` 一并提供。Torrent Collector 以 AnimeMachine 镜像中的 `torrent-collector` 命令运行，对宿主机 Torrent Pool 有写权限；AnimeMachine 对容器内 `/Torrents` 仍保持只读。Collector 使用标题、Torrent manifest 与本地 Catalog 证据执行 `accept / reject / defer` 三态裁决，只有 `accept` 才进入 Torrent Pool。证据不足的资源维持待判断状态，不用固定集数跨度“猜成全集”。现有 Pool 审计默认只报告；若要隔离明确拒绝的文件，需要显式启用隔离，并把隔离目录放在 Torrent Pool 之外。单集、单卷的持续追更仍交给 Ani-RSS 更合适。

普通 `.env` 只保留日常需要修改的变量。代理、轮询间隔、历史回溯批量、最大重试次数、审计模式和 Collector 独立状态目录等参数放在 `deploy/compose/torrent-collector.advanced.env.example`；确有调优需求时再复制对应项目到 `.env`，默认不启用代理。

### fnOS / NAS

fnOS 或类似 NAS 环境建议在 Docker 项目目录准备 `config`、`data`、`imports`、`torrents`、`library`、`external` 六类目录，再按读写特性分配存储：(1)`config`、`data` 放 SSD；(2)`library` 放大容量媒体存储池；(3)`torrents` 映射现有 Torrent Pool，AnimeMachine 一侧保持只读；(4)`external` 映射 ani-rss 或旧媒体目录，同样只读；(5)`.env` 填写 NAS 上真实绝对路径，例如 `/vol1/1000/docker/animemachine/data`；(6)容器使用的 `PUID/PGID` 必须对 `config`、`data`、`library` 具备写权限。

如果部署后出现“页面能打开，但扫描不到文件”或“能读取却不能建立目录”，首先检查宿主机挂载和 `PUID/PGID`，而不是先改 AnimeMachine 的内部路径。容器内部路径是固定映射结果，宿主权限才是更常见的根因。

### 部署与存储分离

AnimeMachine 可以运行在小型主机或虚拟机中，而媒体实际放在另一台 NAS。推荐的完整路径是：NAS 提供 SMB/NFS → AnimeMachine 宿主系统负责挂载 → Compose 只接收本地挂载点。这样 AnimeMachine 不需要长期保存 NAS 账号密码，网络文件系统的重连也交给成熟的系统组件处理。

数据库、Archive 和封面缓存最好留在运行主机的 SSD，只把大容量媒体与 Torrent Pool 放到网络存储。原因很简单：建库、筛选和关系图会频繁读取大量小记录，而视频文件本身才真正需要容量。把两类 I/O 分开，通常比把整个状态目录放 NAS 更稳定。

## 网络与底包

AnimeMachine 的网络层会在官方源、用户配置的镜像和直连之间选择可用路径，并对 Archive 执行大小与 SHA-256 校验。代理设置按请求动态读取，因此运行过程中启用或关闭系统/环境代理后，后续请求可以使用新的路由；回环和私有网段保持直连，避免访问本地 qBittorrent、Ani-RSS 或 NAS 时绕远路。

如果希望完全手动准备底包，可以把官方 `dump-*.zip` 放入 `/Imports`，本地 Release 对应 `imports` 目录，再启动程序导入。公司 HTTPS 网关使用自签名证书时，应把 PEM CA 放入配置目录并设置 `ANM_CA_BUNDLE`。证书验证失败说明信任链没有建立，正确处理方式是补充 CA，而不是关闭 TLS 校验。

## 更新与备份

更新和备份可以记成四条：(1)源码更新后重新运行启动脚本，隔离环境会按当前项目重新安装；(2)Docker 更新使用 `docker compose pull && docker compose up -d`；(3)备份状态目录与配置目录即可保存数据库、封面、字幕、计划和设置，媒体文件仍按用户自己的存储策略备份；(4)任何时候都不要让两个 AnimeMachine 实例同时写入同一个状态目录。

如果需要迁移机器，优先完整复制状态目录和配置目录，再恢复原来的媒体路径或等价映射。只复制 SQLite 而遗漏其它状态文件虽然可能可以启动，但会失去部分缓存、计划或历史上下文，不是推荐的迁移方式。

## 使用指南

### 首次建库

第一次使用时，不建议同时打开所有自动化功能。更稳妥的顺序是：(1)登录后进入“设置 → 连接”，先确认动画收藏库、Torrent Pool 和可选外部媒体库的访问状态；(2)进入“常规”检查并更新动画底库，Archive 在后台分阶段导入，右上角显示实际阶段和进度，完整 Catalog 校验通过后再一次性发布；(3)Torrent Pool 按设定周期增量扫描，已完成批次可以立即查询，如果某部作品暂时没有出现候选资源，可以在作品详情页单独搜索；(4)已经存在大量旧媒体时，先执行本地资源核验，再考虑自动下载。

本地核验分为两个层级。快速模式主要比较规范目标、文件分布和精确字节数，适合日常扫描；存在可比较基准且用户明确启用精确核验时，再进行哈希验证。该机制只在必要位置提高判定强度，避免对全部视频重复计算 SHA-256。

### 选择与下载

首页第一次使用时，作品月份默认覆盖当前月份及之前五个月，例如 2026 年 8 月为 2026-03 至 2026-08；之后用户修改的筛选条件保存在浏览器本地状态。默认每页显示 12 部作品。界面主题跟随操作系统，也可以在语言切换左侧手动选择暗色或浅色模式。

资源选择可以自动和手动混合使用：(1)勾选作品后，系统按当前规则寻找完整度和优先级更合适的方案；(2)需要人工干预时，可以在作品详情页指定某个合集、合卷、多集或单集方案。自动选择并不意味着直接下载，它只是在当前证据下生成候选。

点击“开始下载”后，程序先生成不可变下载计划。计划会列出目标目录、预计空间、准备新增/跳过/暂存替换的文件，以及任务将交给 qBittorrent 还是 Ani-RSS。默认提交 qBittorrent 后保持停止状态；只有用户明确选择自动开始时才立即启动。

资源策略优先完整合集，再考虑单集或单卷拼接。拼接方案必须根据实际覆盖范围判断，已经长期中断更新的发布组会降低优先级。本地已有文件则走差分计划：规范目标相同且字节数完全一致时默认跳过；缺失项只补缺失部分；具备可靠修订证据的旧文件先进入暂存；不是重复内容的原文件继续保留。

### Ani-RSS

Ani-RSS 连接后有三种调用模式：(1)优先调用：本地没有完整合集时优先交给 Ani-RSS；(2)备选调用：只有本地完全没有可用方案时才调用 Ani-RSS；(3)手动调用：只有用户点击“Ani-RSS 查询资源”时才访问。远端暂时不可用时不会改变用户选定的长期模式，连接恢复后仍按原设置工作。

Ani-RSS 管理的媒体在 AnimeMachine 中始终按外部只读来源处理，不直接移动或改名。默认每 30 分钟同步一次 Ani-RSS 订阅状态并扫描已配置的 Ani-RSS 媒体目录；连载作品后来新增且已经下载完成的集数，会在后续同步中进入可播放范围。同一作品如果存在多个 Ani-RSS 订阅，会按远端 ID 独立保留，不互相覆盖。

作品详情页允许删除已有下载内容的 Ani-RSS 订阅，但这是明确的远端写操作：界面会再次确认，然后通过 Ani-RSS HTTP API 请求删除订阅及文件，再重新同步远端列表核对结果。这里的“外部只读”是指 AnimeMachine 不直接改写映射目录，不代表用户明确发出的 Ani-RSS API 删除操作也被禁止。

### 播放

收藏库列表需要先选中一个具体媒体，再使用“复制”“下载播放列表”“使用 VLC 打开”或“使用 PotPlayer 打开”。生成的 M3U 只包含正片，并按集数排序；同一集存在多个媒体副本时，默认播放列表只选择一份，字节数完全相同的副本直接跳过。特典和其它副本仍保留在收藏库记录中。如果从第 N 集开始播放，前面的项目仍保留在列表里，只把播放器起始位置放到用户选择的媒体。

本地运行时，播放器通常可以直接使用文件路径；Docker 或远程部署则未必。容器内部的 `/Library` 对用户电脑没有意义，因此“设置 → 外部播放器交接”需要提供客户端可以访问的 HTTP 地址，或者建立服务器路径到客户端路径的映射。例如服务器 `/Library` 可以映射到 Windows 的 `\\nas\anime`，也可以映射到 macOS 的 `/Volumes/anime`。只有路径真的能从播放端访问，直接调用本地播放器才有意义。

### 字幕

连载类资源在候选选择阶段优先匹配用户语言字幕；归档类资源则按顺序检查：(1)媒体内嵌字幕；(2)同名外挂字幕；(3)本地字幕压缩包；(4)用户自行配置的字幕服务。选择新的字幕文件时，旧字幕先备份到状态目录；媒体如果来自外部只读库，新字幕也保存到 AnimeMachine 状态目录，不写回原媒体路径。

因此，字幕处理和媒体所有权是分开的：AnimeMachine 可以为一个只读视频建立播放时的字幕关联，但不会因为“找到了更好的字幕”就获得修改外部媒体库的权限。

### 目录与历史

单部作品目录默认使用 `『YYYY_MM』『作品原名』`；系列目录使用 `『开始－结束』『「系列根名」シリーズ』`，系列内的子作品仍按各自首播年月命名。分割放送如果属于同一官方季度，会归并到首个季度；迷你动画、Picture Drama 等内容如果具有明确本篇归属，则作为附件处理，不另外创建占位目录。

目录处理以确认作品身份和已有路径为前提。AnimeMachine 对用户原有文件执行移动、改名或替换前，会把变更写入“设置 → 历史记录”；程序自己新建的普通目录不重复记录。如果目录占用、合集边界或作品身份仍然存在无法自动证明的冲突，下载计划会停在审核状态，等待取得充分证据后继续。

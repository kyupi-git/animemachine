[中文](guide.md) | [English](guide.en.md) | [日本語](guide.ja.md)  
[README](../README.md) · [架构说明](architecture.md) · [配置参考](reference.md)

# 部署与使用指南

本指南介绍 AnimeMachine 的部署、首次设置、日常使用及维护方法。按设备选择启动方式，登录后配置所需的媒体目录与服务连接。

[选择启动方式](#start) · [首次使用](#first-use) · [日常使用](#daily-use) · [连接与目录](#connections) · [更新与备份](#maintenance) · [常见问题](#help)

<a id="start"></a>

## 1. 选择启动方式

### Windows

1. 打开 [Releases](https://github.com/kyupi-git/animemachine/releases/latest)，在 Assets 中下载名称带 `release-windows.zip` 的文件。
2. 将 ZIP 完整解压到一个固定目录，例如 `D:\AnimeMachine`。
3. 双击解压目录中的 **`AnimeMachine.cmd`**，保留启动窗口。
4. 在浏览器打开 **<http://localhost:8787>**。

发布包包含 Python 运行环境。后续仍通过同一个文件启动；关闭启动窗口即可停止程序。

### Linux / macOS

先安装 [Python 3.11 或更新版本](https://www.python.org/downloads/)，再从 [Releases](https://github.com/kyupi-git/animemachine/releases/latest) 下载与系统和处理器对应的包。`x86_64` 通常对应 Intel / AMD，`arm64` 或 `aarch64` 对应 ARM；Apple 芯片选择 ARM 包。

解压后，Linux 在解压目录打开终端，运行：

```sh
./AnimeMachine-Linux.sh
```

macOS 打开 **`AnimeMachine-macOS.command`**。也可以在解压目录的终端运行：

```sh
./AnimeMachine-macOS.command
```

启动器会安装所需组件。随后打开 **<http://localhost:8787>**。关闭终端或按 `Ctrl+C` 停止程序。遇到执行权限提示时，先运行 `chmod +x AnimeMachine*.sh AnimeMachine-macOS.command`。

<a id="docker"></a>

### NAS / Docker

先在 NAS 的应用中心启用 Docker / 容器管理，或在电脑上安装 [Docker](https://docs.docker.com/get-started/get-docker/)。使用 Docker Engine 24+、Docker Compose **2.20.3+**。

初次使用可先阅读[图文分步的 Docker Compose 指南](../deploy/compose/README.md)。默认使用[完整组合](https://github.com/kyupi-git/animemachine/blob/main/deploy/compose/compose.yaml)，一次启动 AnimeMachine、qBittorrent、Ani-RSS 和资源采集器。已有服务时，从下表选择一个替代方案；01–04 是不同组合，选择其中一个即可。

| 适用场景 | 配置文件 |
| --- | --- |
| 先浏览作品，接入已有媒体 | [01 · AnimeMachine](https://github.com/kyupi-git/animemachine/blob/main/deploy/compose/templates/01-animemachine-standalone/compose.yaml) |
| 已经有 qBittorrent | [02 · 接入现有下载器](https://github.com/kyupi-git/animemachine/blob/main/deploy/compose/templates/02-animemachine-external-qbt/compose.yaml) |
| 同时部署 qBittorrent | [03 · AnimeMachine + qBittorrent + 资源采集器](https://github.com/kyupi-git/animemachine/blob/main/deploy/compose/templates/03-animemachine-managed-qbt/compose.yaml) |
| 同时部署下载器、Ani-RSS 和资源采集器 | [04 · 完整组合](https://github.com/kyupi-git/animemachine/blob/main/deploy/compose/templates/04-full-stack/compose.yaml) |

1. 新建一个固定目录，例如 `animemachine`。
2. 打开上表中的配置文件，点击 **Download raw file**，保存到该目录，文件名保留为 **`compose.yaml`**。
3. 在这个目录打开终端，执行下面的命令。在飞牛、群晖等 NAS 的 Compose 项目页面，也可以直接粘贴配置内容并启动。

```sh
docker compose up -d
```

4. 打开 **`http://NAS地址:8787`**。在本机部署则打开 **<http://localhost:8787>**。
5. 在容器管理页面查看 `animemachine` 的日志，获取首次登录账户和密码。使用终端时运行：

```sh
docker compose logs animemachine
```

默认目录和服务连接会自动建立。03 / 04 自动配置 qBittorrent，04 同时完成 Ani-RSS 连接；选择 02 时，按[连接说明](#connections)填写已有下载器的信息。

需要把媒体保存到指定磁盘时，在启动前按[目录设置](#folders)增加 `.env`。qBittorrent 与 Ani-RSS 默认可从局域网打开，地址及初始密码见[管理页面访问](reference.md#service-access)。

每种方案只需一个 `compose.yaml`，无需先运行初始化脚本或填写密钥。服务连接与账户在首次启动时自动建立，重启沿用已有设置。完整组合会检测蜜柑与公开作品资料的连接情况，优先使用官方来源，连接异常时选择可用备用来源。

<a id="first-use"></a>

## 2. 首次使用

### 登录并确认作品列表

启动窗口或容器日志会显示访问地址、初始用户名和随机密码。登录后，页面显示作品资料的准备进度：先下载并校验底包，再准备当季及近期作品和封面，其余资料在后台补全。浏览、订阅和播放可先使用；收藏目录与本地下载计划在完整资料就绪后启用。中途退出后会继续准备，升级时已有完整作品库直接使用。

在 **设置 → 用户** 中可为其他使用者创建账户。也可以创建新的管理员账户，使用新账户登录后停用初始账户。

### 设置媒体目录并验证

在 **设置 → 连接** 中，按用途填写目录：

| 用途 | 设置项 |
| --- | --- |
| 整理新下载的收藏 | 本地收藏库下的“动画收藏库路径” |
| 浏览硬盘上已有的动画 | 启用“映射外部只读媒体库”，填写该媒体目录 |
| 接入 Ani-RSS 下载的动画 | Ani-RSS 下的“媒体路径”；也可直接使用其远程播放能力 |

Docker 中填写程序看到的容器路径；例如默认收藏库是 `/Library`，已有媒体是 `/External`。[目录示意图](#folders)给出了完整例子。

![本地收藏库设置：填写目录，并按需要设置每日休眠时段](images/library-settings.png)

保存配置后，搜索一部作品并打开详情，确认已识别的媒体文件可以播放。如需追番，按下一节添加一部订阅并检查同步结果。完成一次播放或订阅验证后，再添加其他作品。

![作品详情：资料、集数和已有媒体](images/work-detail.png)

<a id="daily-use"></a>

## 3. 日常使用

### 浏览与筛选

首页可以切换卡片或表格，按年代、月份、类型、制作公司、主题、资源来源和收藏状态筛选。中文、繁体中文、英文、日文名称都可以用来搜索。

作品卡片上的 **资源可用 / 已订阅** 表示资源或追番状态，**暂未入库 / 部分入库 / 全部入库 / 外部只读** 表示媒体收藏状态。打开详情可查看具体集数和文件；“新作追更”用于查看更新，“随机”用于浏览作品。

![浏览作品：左侧筛选，右侧查看封面、订阅与收藏状态](images/library-overview.png)

### 新作雷达与追番

首页“新作追更”和雷达依次显示 TV、剧场版、OVA、网络动画和其它作品。同类型中，已开播作品按最近活动时间排列，未开播作品按预计日期由早到晚排列。最近活动取订阅时间与最新一集资源更新时间中较晚的一个。

先[连接 Ani-RSS](#ani-rss)，再按下面的顺序操作：

1. 打开 **新作雷达**，选择上季度、本季度或下季度；每个季度的作品在同一页展示。
2. 点击 **同步状态**，更新订阅、媒体及所选季度的资源状态。**检索资源**用于依次检查本季度作品，进度会立即显示；个别作品失败后，其他作品会继续检索，可再次点击重试未完成作品。
3. 找到想追的作品，点击 **订阅**，选择资源或字幕组，再确认订阅。
4. 下载由 Ani-RSS 执行。提交订阅期间可关闭窗口继续浏览，订阅状态和媒体集数会自动同步回 AnimeMachine。

![新作雷达：查看季度更新、管理订阅并继续观看](images/release-radar.png)

订阅窗口中选择字幕组，再点击 **启用订阅**。完成后，雷达中的按钮会变为 **已订阅**。

![添加订阅：选择资源组并确认](images/subscribe-release.png)

资源与语言、字幕组或画质设置不符时，会显示具体原因。仍想使用该资源，可在作品详情中选中，再点击 **强行订阅 / 强行下载**，核对预览并提交。雷达的订阅窗口也可直接选择 **强行订阅**。此操作只对当前选择生效，不会修改设置。

本季度发售 BD 的剧场版也会进入雷达，并在开播日期下显示 BD 日期。缺少明确 BD 日期时，检索会补查近一年上映的剧场版，按 Ani-RSS 返回的最早资源发布时间归入季度，并标注“资源”日期。

“最新 / 总集数”的最新集数优先使用 Ani-RSS 的资源和订阅记录；“新集更新时间”在有记录时显示。“开始时间”合并日期和类型，例如 `2026-10-05（TV）`。点击“开始时间”等表头，可对整个季度重新排序，再点一次反向排列。

点击“已订阅”或“已暂停”菜单，可暂停、恢复或取消订阅。取消订阅会保留已下载文件；“删除订阅及文件”会另行确认。新订阅会在所属分组中前移，首页的年代、月份等筛选仍然生效。

雷达的 **已看集数** 显示上次播放的集数，点击数字或“—”即可修改。浅绿色标出已订阅或已有媒体作品中的待看新集；播放最新集或修正记录后更新，打开详情不会消除高亮。

在作品的订阅菜单中选择 **隐藏作品**，可将它从当前账号的雷达、作品列表和新生成的续播列表中移除；订阅和下载继续运行。点击雷达顶部的 **已隐藏作品** 可搜索并还原，首页标题搜索也能找到已隐藏的作品。

### 一键继续观看

在 **新作雷达** 中选择季度，点击 **一键继续观看**，勾选要看的作品，再打开播放器。可在每行的「起始」「结束」框中修改播放范围（例如将默认的 2–4 集改成 1–4 或 3–4 集），拖动作品行调整顺序。播放列表只包含已勾选的作品和指定范围内的可播放集数，按从上到下的顺序播放；未勾选时播放按钮不可用。若确认期间媒体或观看进度已变化，重新打开续播列表以刷新可播放范围。列表收录已订阅或已有媒体的 TV 动画，每部从上次播放的那一集开始，直到最新可播放集；首次观看则从最早可播放集开始。已全部看完的作品默认不播放，可填写起始、结束集数重新观看。

![一键继续观看：确认本季可播放的集数与续看位置](images/continue-watching.png)

观看记录与账号绑定，作品详情和雷达共用。点击雷达中的 **已看集数**，或作品详情中的 **续看：第 X 集**，可修改起始集和播放位置、设为未开始或撤销修改。打开播放列表或修改播放范围本身不会更新记录；实际播放时记录观看进度，已播放 ≥99% 的一集视为完成、≤1% 不计为观看，支持精确位置的播放器扩展会保存播放时间。需要时可手动纠正。未启用账号登录时，所有设备共用一份记录。

![修改续看记录：填写起始集数和播放位置后保存](images/viewing-record.png)

**PotPlayer**：点击播放区的 **安装扩展以记忆播放进度**，退出播放器，将 ZIP 内的 `Extension` 文件夹合并到安装目录，重新打开播放器后自动生效。扩展在 64 位播放器内记录实际播放位置，下次从上次停止位置前五秒开始，不足五秒则从头播放。网页直接打开、复制地址和下载列表均可使用。未安装扩展时，列表仍可正常播放，按集数记录观看情况。

![连续播放：PotPlayer 侧栏显示多部新作的播放顺序](images/potplayer-weekly.png)

**VLC**：无需安装上述扩展，默认记录观看集数。VLC 自身保存的本地播放位置由 VLC 管理，与 AnimeMachine 的账号记录分开。

Windows 的 Edge、Chrome 每次打开播放器都要求确认时，可点击 **复制授权命令**，按 `Win + R`，输入 `cmd` 并回车，粘贴命令运行一次，再重开浏览器。命令仅为当前站点添加 PotPlayer、VLC 调用权限，保留已有授权。

### 补齐收藏与下载

连接 qBittorrent 后，在作品详情点击 **检索 Torrent**。检索完成后会定位到候选资源：选择一项，再点击 **预览下载计划**，核对文件和保存位置后提交。已找到但未确认作品匹配的资源会说明原因，暂不能提交下载。

资源分为 **Ani-RSS 订阅** 和 **Torrent 下载**，后者默认折叠，检索完成后自动展开。同一字幕组显示在一行，通过下拉框选择平台、字幕和画质，例如 Baha / CR、简体 / 繁体、1080p / 720p，再选中资源并确认。Ani-RSS 后续仅使用所选资源组和版本；更换版本时沿用已有文件、保存目录和进度。

![资源选择：同一字幕组按平台、字幕和画质区分版本](images/resource-variants.png)

已有 `.torrent` 文件放入 **Torrent 池**目录。完整 Compose 组合自带资源采集器，会在后台补充此目录。**设置 → 连接 → Torrent Collector** 可选择作品检索来源、调整数量与时限，或关闭定时扫描、自动采集和历史补充。资源收录与排序沿用已有资源偏好。

正在连载的作品会核对季度和连续集数：从第 1 集开始，最多落后 Ani-RSS 最新记录一集；超出当前进度或中间缺集的版本不进入候选。已完结作品和剧场版仍可用于补齐收藏。

1. 搜索作品，在详情中查看候选资源和已有文件。
2. 勾选要收藏的作品，生成计划，检查将下载的文件和目标目录。
3. 确认后，计划交给 qBittorrent，任务以**停止状态**加入。
4. 打开 qBittorrent，选中任务并开始下载。下载完成后，AnimeMachine 会更新收藏状态。

计划会比较已有文件，选择需要补齐的内容。资源偏好可在 **设置 → 资源优先级 / 资源组** 中调整。

单集、单卷任务完成后，还可以在 **设置 → 订阅** 中查看后续发布监视；发现新资源后会产生待确认项。

### 建立订阅目录与核验收藏

连接可读写的动画收藏库并保存设置后，点击新作雷达中的 **建立目录**，即可为已订阅作品建立占位目录。已有目录会继续使用，重复操作会跳过已经建立的目录。

也可在 **设置 → 常规 → 收藏库** 中建立目录或 **核验全部本地资源**。处理会在后台进行，页面显示进度和结果；完整度按当前文件核验。

在 **设置 → 常规 → 目录调整预览** 中，可核对已登记目录的命名、日语版标题和附属短篇归属，并下载检查结果。预览只读取现有目录。

### 设置本地库休眠

存储状态采用只读检查，写入权限在建立目录或整理文件等实际操作时确认。

在 **设置 → 连接 → 本地收藏库** 启用每日休眠时段，填写开始、结束时间并保存。使用运行设备的本地时间，支持例如 23:00 至次日 08:00。

期间暂停对本地库的探测、扫描、核验、播放和写入，页面继续显示已有记录；时段结束后恢复访问。Ani-RSS 和其它路径的外部库继续运行。若其它程序或媒体库使用同一块硬盘，它们仍可能唤醒硬盘。

配置、数据库和日志也建议保存在其它磁盘，以便媒体磁盘在休眠时段保持空闲。

### 作品关系图

在卡片或详情中点击 **作品关系图**，查看前作、续作、剧场版、番外等关系。可以拖动、缩放、全屏查看，筛选关系类型，或导出图片。

![作品关系图：顺着系列寻找关联作品](images/relationship-graph.png)

### 播放与字幕

打开作品详情，选择播放来源和起始集，再点击 VLC、PotPlayer 或系统播放器。macOS 还可以使用 IINA。**复制播放列表**可以把整季地址交给其他播放器。

![播放：选择起始集并打开播放器](images/playback.png)

播放期间保持 AnimeMachine 运行；重新启动后请重新生成播放列表。播放器安装在正在观看的设备上。跨设备播放时，在 **设置 → 连接 → 外部播放器交接** 中填写该设备能访问的 AnimeMachine 地址，例如 `http://nas.example:8787`；能直接访问共享媒体目录时，也可以设置 SMB / NFS 路径映射。

字幕可以使用媒体内嵌字幕、同目录外挂字幕，或在详情中导入字幕文件与压缩包。需要在线查找时，在 **设置 → 连接 → 字幕源** 中填写 ASSRT 或 OpenSubtitles 的账户密钥，再搜索和选择匹配结果。

<a id="connections"></a>

## 4. 连接与目录

<a id="ani-rss"></a>

### 接入已有 Ani-RSS

在 Ani-RSS 的 **设置 → 安全** 中找到 API Key，复制到 AnimeMachine 的 **设置 → 连接 → Ani-RSS**：

| 项目 | 填写内容 |
| --- | --- |
| 服务地址 | AnimeMachine 能访问的 Ani-RSS 地址，例如 NAS 上的 `http://192.168.1.10:7789` |
| API Key | 从 Ani-RSS 复制的密钥 |
| 调用模式 | 一般选择“优先调用 Ani-RSS” |
| 媒体路径 | AnimeMachine 可读取的 Ani-RSS 下载目录；仅使用远程 API 时可留空 |

Docker 中的 `localhost` 指容器自身。同一 Compose 组合内可使用 `http://ani-rss:7789`；连接已有服务时填写其电脑或 NAS 地址。

![连接 Ani-RSS：填写自己的服务地址与 API Key](images/connect-ani-rss.png)

保存后点击 **测试连接**。连接成功后，资源、订阅和媒体状态会自动同步。完整 Compose 组合已完成这些连接。

Ani-RSS 与它使用的下载器应看到相同的媒体保存路径。其下载设置和订阅选项可查看 [Ani-RSS 下载说明](https://docs.wushuo.top/config/download)与[订阅说明](https://docs.wushuo.top/add-rss)。

### 接入已有 qBittorrent

使用 **qBittorrent 5.2.0 或更新版本**。在 qBittorrent 的 Web UI 设置中启用 Web UI，并创建或复制 API Key。

在 **设置 → 连接 → qBittorrent** 中填写服务地址、API Key、动画收藏库路径和 qBittorrent 看到的收藏库路径，保存并点击 **测试连接**。Docker 连接宿主机上的下载器时，地址通常使用 `http://host.docker.internal:8080`；连接同一 Compose 中的下载器则使用 `http://qbittorrent:8080`。

<a id="folders"></a>

### 目录与路径映射

同一个媒体目录在 NAS、AnimeMachine 容器和下载器中可能使用不同的路径。**Compose 配置使用宿主机目录，程序设置使用容器内路径。**

![同一个收藏目录，在宿主机和两个容器中的路径](images/folder-mapping.svg)

默认 Compose 目录如下，左侧相对路径以 `compose.yaml` 所在目录为起点：

| 宿主机目录 | AnimeMachine 内的路径 | 用途 |
| --- | --- | --- |
| `./library` | `/Library` | 新下载和整理的收藏 |
| `./torrents` | `/Torrents` | `.torrent` 文件 |
| `./external/read-only` | `/External` | 已有媒体库，按只读方式浏览与播放 |
| `./external/ani-rss` | `/Media` | Ani-RSS 下载的媒体 |
| `./config`、`./data` | `/Config`、`/Data` | 设置、账户和运行记录 |

例如把收藏保存到 `/srv/anime`，在 `compose.yaml` 同目录新建 `.env`，写入：

```dotenv
ANM_LIBRARY_DIR=/srv/anime
```

然后执行 `docker compose up -d`。AnimeMachine 设置中的收藏库路径仍然是 `/Library`。外部库、数据目录等可用同样方式调整，变量见[配置参考](reference.md#environment)。

直接运行 Windows 时，可填 `D:\Anime` 或 `\\nas\Anime`；Linux / macOS 先挂载 NAS 共享，再填挂载目录。将设置和运行数据放在运行程序的本地磁盘上，媒体可以放在 NAS。现有媒体库选择“外部只读媒体库”，就可以沿用原目录结构。

<a id="maintenance"></a>

## 5. 设置、更新与备份

**设置 → 历史记录**可按目录搜索建立、移动、改名和移除记录，并还原可撤销的操作。**设置 → 日志**可只看警告和错误，需要进一步检查时再展开技术详情。

更新时保留原来的部署目录和数据，在同一目录替换 `compose.yaml`，执行 `docker compose up -d`。只有从其它组合切换、确认不再需要旧服务时，才加 `--remove-orphans`；配置和媒体仍使用原挂载目录。

语言、主题、布局和浏览筛选可以按自己的习惯调整。管理员在设置中管理目录、连接、资源规则和用户；普通账户用于浏览与播放。

作品资料会每周自动检查 Bangumi Archive 更新，也可以在 **设置 → 常规 → 检查并更新底库** 中立即检查。已下载的 Archive ZIP 可以通过 **导入已下载的底包**使用。代理和自定义证书见[网络设置](reference.md#ANM_CA_BUNDLE)。

在 **设置 → 检查更新** 中查看新版本，阅读发行说明并确认更新。默认每天定时检查并通知；检查时间和自动安装选项可在此调整。

Docker 更新完整镜像时，在原 Compose 目录运行；使用固定版本的配置先把镜像版本改为准备升级的版本：

```sh
docker compose pull
docker compose up -d
```

备份时，先停止 AnimeMachine，再复制下列内容：

| 部署方式 | 备份内容 |
| --- | --- |
| Windows / Linux / macOS 发布包 | `config.json`、`.env.local` 和整个 `data` 目录 |
| Docker | Compose 文件、`.env`（如有）、整个 `config` 和 `data` 目录；自定义的采集器数据目录或命名卷也一并备份 |
| 两种方式都适用 | 另行保存媒体和 Torrent 池 |

恢复时，把这些内容放回相应位置，再启动同一份配置。迁移设备后，按新设备的实际路径调整媒体目录。目录移动、改名和版本替换的记录，可在 **设置 → 历史记录** 中查看和恢复。

<a id="help"></a>

## 6. 常见问题

| 情况 | 处理方法 |
| --- | --- |
| 忘记初始密码 | 本地发布包查看 `data/state/auth/initial-admin.txt`；Docker 查看 `data/state/auth/initial-admin.txt` 或容器日志。该文件记录首次生成的账户。 |
| 作品列表暂时为空 | 查看初始化进度，等待作品资料导入；在“诊断”中查看网络和底包状态。 |
| 有作品但没有封面 | 封面在后台补齐，可继续浏览作品；进度可在“诊断”中查看。 |
| 没有识别到已有动画 | 确认媒体目录在运行 AnimeMachine 的设备上可读取；Docker 检查挂载，再检查 `/External` 等容器路径。 |
| 新作集数或订阅没有更新 | 测试 Ani-RSS 连接，点击“同步状态”，并检查 Ani-RSS 中该订阅的更新情况。 |
| 提示磁盘预留空间不足 | 在“设置 → 常规 → 自动化与容量”中核对最低预留空间，再确认磁盘可用容量。 |
| qBittorrent 中的任务没有开始 | 确认计划已提交，打开 qBittorrent 手动开始停止的任务。 |
| 播放器没有打开 | 安装对应播放器，允许浏览器打开外部应用；也可复制播放列表，在播放器中打开。 |
| 手机或另一台电脑无法播放 | 在“外部播放器交接”中填该设备可访问的地址，并测试它能打开 AnimeMachine 页面。 |
| 地址或端口被占用 | 按[端口设置](reference.md#ports)改用另一个端口，再重新启动。 |

进一步检查时，打开 **设置 → 诊断 / 日志**，查看对应组件的当前状态和最近错误。网络恢复后，后台任务会继续重试。

排查问题时，可在启动前设置 `ANM_LOG_LEVEL=DEBUG`，显示详细耗时；平时使用默认输出即可。

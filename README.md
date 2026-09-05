[中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)  
[README](README.md) | [部署与使用指南](docs/guide.md) | [架构与数据库](docs/architecture.md) | [更新日志](CHANGELOG.md)

# AnimeMachine · Automated Anime Library

AnimeMachine 是一款全自动动画收藏库系统，负责把动画元数据、Torrent 池、媒体目录和外部只读媒体库整理到同一套本地 Catalog 与状态体系。**AnimeMachine 本身不提供动画检索或下载**，也不内置 Torrent、磁力链接或媒体内容，由用户设置本地动画收藏库的路径或挂载外部只读库，映射自己的 Torrent Pool 或使用 Torrent Collector 从公共索引收集符合规则的合集类 `.torrent` 元数据，并自行连接 qBittorrent、Ani-RSS 等组件完成实际下载或订阅。随着动画新作的持续播出，用户收藏的持续增长，AnimeMachine 的工作重点体现在对资源来源、目录命名、媒体文件、作品信息、系列关系、既有收藏等内容的一致性维护，能实现超大量级的动画收藏库的持续性管理。

如果把一部动画从“发现资源”到“进入收藏库”的过程拆开，AnimeMachine 主要连续完成七项工作：(1)原材料筛选：扫描 Torrent 池，先判断是不是完整资源，再比较合集形式、资源组、片源、分辨率等条件；(2)投料：把用户确认的任务交给 qBittorrent，或者把连载订阅交给 Ani-RSS；(3)初加工：根据首播年月、正式标题和系列归属规划多级目录；(4)精加工：建立前作、续作、总集篇、番外、衍生、不同演绎、跨系列关联和角色出演等逻辑关系；(5)质检：把 Torrent manifest 与本地文件逐项比较，只补缺失内容，并且只有存在可靠修订证据时才暂存和替换旧文件；(6)包装：通过中、英、日三语 Web 页面统一浏览动画目录、资源候选、收藏状态和系列关系；(7)贮存：把已经下载、外部映射和等待下载的作品，组织成一套可以继续扩展和维护的动画仓库。

![AnimeMachine 收藏库首页](docs/images/library-overview.png)

*收藏库首页：默认展示最近播出的动画，支持多种方式筛选和排序。*

![AnimeMachine 作品详情页](docs/images/work-detail.png)

*作品详情页：展示一部动画的主要信息，均读取自 Bangumi Archive。*

![AnimeMachine 播放器交接](docs/images/playback.png)

*AnimeMachine 的播放器交接：在检测到可用媒体资源后，AnimeMachine 采用一种“生成 m3u 播放列表并交接”的独特方式，推送全集地址至本地播放器，可以实现全集自动跳转播放，并可从中间集数开始播放。该方式兼容多种平台、多种设备，只要安装了 VLC / PotPlayer 等支持 m3u 播放列表的本地播放器。甚至，即便 AnimeMachine 未挂载 Ani-RSS 媒体目录，只要通过 API key 连接 Ani-RSS，仍然可以通过 AnimeMachine HTTP 中继生成 m3u 播放列表并播放，并支持 Range 拖动和短暂断线续传。同时，也支持直接控制 Ani-RSS 订阅新作。*

![AnimeMachine 作品关系图](docs/images/relationship-graph.png)

*作品关系图：这是 AnimeMachine 根据数据库记录的作品间的逻辑关系，自动生成的可视化图形。由于逻辑关系是多对多，甚至存在多层嵌套，关系图的绘制是极其复杂的，要优化作品节点的摆放坐标，要避免多条关系线潜在的轨道碰撞，还要鉴别原始关系数据的错误和缺失，利用旁证推导正确的关系网，最后算法的运行时间要可接受。*

## 主要能力

- 基于 Bangumi Archive 的动画底库。首次启动会在后台构建本地 SQLite Catalog，完整性检查通过后再原子发布。
- Torrent 池采用增量扫描。已经处理且指纹未变化的文件不会重复解析；即使大型 Torrent 池仍在全量扫描，已经完成的批次也可以先用于查询和资源选择。
- Docker 自动收藏方案支持启用 Torrent Collector。Collector 结合标题、Torrent manifest 和本地 Catalog 证据执行 `accept / reject / defer` 三态判断：只有 `accept` 写入共享 Torrent 池，证据不足时保持 `defer`，而不是用固定集数跨度猜测完整。每周更新的单集资源可以向 Ani-RSS 发起订阅请求并远程访问。
- 下载规划支持完整合集、单集/单卷拼接、差分补完，以及同一 infohash 追加文件选择。AnimeMachine 管理的 qBittorrent 任务会先生成计划，并始终以停止状态提交；确认内容后再由用户在 qBittorrent 中启动。
- 收藏库既可以是本地媒体目录，也可以是可读写 UNC/NAS 目录；已有媒体还可以作为外部只读媒体库映射进来，不要求搬迁或重新命名。
- qBittorrent、Ani-RSS 和 Torrent Collector 都是可选组件。可以只使用其中一部分，也可以在 Compose 中组成完整自动收藏链路。
- 0.2.1 对远程 Ani-RSS 采用“当前凭据 + 端点 + 实际代理/直连路由”健康门控：代理或 `NO_PROXY` 变化后旧 `ready` 快照立即暂时退出当前资源/播放来源，并触发补偿同步；重新验证成功后自动恢复。Ani-RSS 不可用期间 Torrent、Bangumi 图片、本地收藏与独立挂载的只读媒体仍继续工作，本地/LAN Ani-RSS 始终直连。成功同步若发现 Ani-RSS 已为作品提供明确封面，还会解除此前由自有图片源留下的 `no_cover` 负缓存，使下一次正常图片请求即可转用 Ani-RSS，而无需等待图片维护周期。 切换 Ani-RSS 端点或 API Key 时，上一实例的资源搜索缓存会立即失效；旧实例仍在执行的搜索即使晚到也会因来源代次不匹配而丢弃，新实例重新验证后资源扫描立即重新到期，避免旧候选短暂重新出现。远程资源搜索还会固定其开始时的有效代理路由代次；若代理或 `NO_PROXY` 在搜索途中换代，本轮结果会在写入前被丢弃，避免慢请求跨网络环境回灌。
- Ani-RSS 的“资源调用模式”只控制资源发现与规划；即使选择“手动调用”，已连接的订阅与 API 可播放媒体仍按同步间隔更新。localhost/LAN 的封面故障冷却与无关代理切换解耦，避免频繁开关代理导致本地 `/api/file` 提前重试。
- 播放功能生成 M3U 播放列表，再交给系统播放器、VLC 或 PotPlayer；服务器路径与客户端路径不一致时，可以单独配置播放器可访问的映射。
- 对归档类资源，程序可以检查内嵌/外挂字幕，并连接用户自行配置的字幕服务；外部只读媒体不会因为字幕处理被改写。
- 支持生成作品关系图，呈现动画在所属系列的位置，系列中各作品的逻辑关系。在作品关系图中，可以点击跳转其它关联作品；节点标题与卡片、详情页使用同一套当前界面语言规则。0.2.1 会在升级现有 Catalog 时重新核对已保存的英文别名，剔除任何含非拉丁字母、却被误标为英文的别名（包括中/日/韩、西里尔、希腊、阿拉伯等文字）并补齐可验证的英文显示标题；上游确实没有英文名时，英语界面会临时回退显示作品原标题，但不会把该原标题写入或标记为英文标题；一旦后续取得可靠英文名会自动优先使用，无需重建 Catalog。

## 快速开始

### 本地运行

Windows 用户运行 `scripts/windows/AnimeMachine.cmd`；Linux 用户运行 `scripts/unix/AnimeMachine-Linux.sh`；macOS 用户运行 `scripts/unix/AnimeMachine-macOS.command`。从源码首次启动时，需要预先安装 Python 3.11 或更高版本；启动脚本会建立隔离环境并安装当前项目。无需预先填写 `.env.local`：默认监听 `0.0.0.0:8787`，本机使用 `http://127.0.0.1:8787`，局域网其它设备使用 `http://<主机IP>:8787`；零配置首次启动会自动生成管理员账号和密码并保存到受限凭据文件，只要该初始管理员仍有效，后续启动也会在控制台再次显示这组登录信息。

详细步骤见 [部署与使用指南](docs/guide.md#本地部署)。Release 同时保留 Windows、Linux 和 macOS 三套启动入口，但只有构建平台的运行时或依赖会随包直接提供；换到其它系统运行时，需要预装 Python 3.11 或更高版本，首次启动也可能联网补充当前平台的兼容依赖。

### Docker Compose

`deploy/compose` 提供四种预设方案：(1)AnimeMachine 独立运行；(2)连接外部 qBittorrent；(3)启用 Torrent Collector 和内置 qBittorrent；(4)把 Torrent Collector、qBittorrent 与 Ani-RSS 一并纳入同一 Compose 项目。前三种方案均可按需连接外部 Ani-RSS，未配置时不影响动画底库、目录、Torrent Pool 与本地收藏管理。

0.2.1 的预设方案默认固定公开镜像 `ghcr.io/kyupi-git/animemachine:0.2.1`。完整 Compose 同时固定 Ani-RSS `v3.2.28` 与 qBittorrent `5.2.3`，当前适配层已按这两版接口核验；外接 qBittorrent 继续要求 5.2.0 或更新版本。若自行改为 `latest`，Compose 会跟随后续发布；生产环境建议继续固定具体版本标签。

进入对应目录后可直接运行 `docker compose up -d`，不要求先创建 `.env`；默认公开 `0.0.0.0:8787` 并自动生成 AnimeMachine 管理员密码，第 3/4 套方案还会自动生成并持久复用内置 qBittorrent/Ani-RSS 的随机 API 密钥，并让 AnimeMachine 等待密钥 bootstrap 成功、托管服务容器开始运行后再进入首次应用启动，避免首次零配置部署的启动竞态。`04-full-stack/compose.yaml` 本身是完整的单文件部署定义，即使只把这一个 YAML 复制到空目录、同目录没有 `.env`，也能按默认相对目录完成首次部署；Ani-RSS 的最终下载位置默认落在共享 `/Media`（宿主机 `./external/ani-rss`），qBittorrent 的 `/downloads/incomplete` 仅用于未完成数据，因此下载完成后 AnimeMachine 无需追加路径配置即可识别并播放这些媒体；只有需要覆盖宿主机路径、凭据、PUID/PGID、代理或端口时才复制 `.env.example` 为 `.env`。完整配置见 [部署与使用指南](docs/guide.md#docker-compose)。

## 文档

- [部署与使用指南](docs/guide.md)：本机、NAS、Compose、首次建库、下载、Ani-RSS、播放和日常维护。
- [架构与数据库](docs/architecture.md)：模块边界、数据流、目录规则、关系图算法、SQLite 实体和状态原则。
- [第三方与数据边界](THIRD-PARTY.md)；[安全策略](SECURITY.md)；[参与开发](CONTRIBUTING.md)。

## 构建与清理

Windows：

```text
scripts\windows\Build-Release.cmd
scripts\windows\Build-Docker-Image.cmd
scripts\windows\Clean-AnimeMachine.cmd
```

Linux/macOS：

```bash
./scripts/unix/build-release.sh
./scripts/unix/build-docker-image.sh
./scripts/unix/Clean-AnimeMachine.sh
```

所有构建产物统一写入 `dist`。清理脚本仅处理构建、测试和解释器缓存，并保留数据库、封面缓存、用户配置与收藏历史；其用途是清理开发产物，而非重置 AnimeMachine。

## 数据与许可

动画底库来自 [Bangumi Archive](https://github.com/bangumi/Archive)，Ani-RSS 的连接方式参考 [ani-rss](https://github.com/wushuo894/ani-rss)。第三方组件、数据来源和许可边界见 [THIRD-PARTY.md](THIRD-PARTY.md)。

AnimeMachine 以 AGPL-3.0-only 发布。

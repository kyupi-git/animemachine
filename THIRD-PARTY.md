# 第三方组件与数据边界

AnimeMachine 是通用的动画目录、文件组织和下载编排工具，只负责处理用户自行提供或自行配置的数据源。项目本身不附带媒体文件、Tracker 定义、Tracker 凭据、Torrent 文件、磁力链接、私有目录，也不预置由 Bangumi 数据派生的数据库。用户需要自行确认所在司法辖区内各项数据源和下载行为的合法性与安全性；这里描述的是产品边界，不构成法律意见，也不能替代针对具体项目的专业法律审查。

| 项目或数据源 | 集成边界 |
|---|---|
| [Bangumi Archive](https://github.com/bangumi/Archive) | 运行时下载并作为 Catalog 底库，同时保留明确的来源和归属说明。由于该仓库目前没有提供清晰的数据再分发许可，在未取得单独授权或完成许可审查前，源代码仓库、Release 和镜像均不打包派生快照或缓存图片。 |
| [qBittorrent](https://github.com/qbittorrent/qBittorrent) | 独立分发的 GPL 下载客户端，AnimeMachine 只通过 Web API 管理用户明确批准的 AnimeMachine 任务；镜像中的相关声明和源码链接必须保留。 |
| [Ani-RSS](https://github.com/wushuo894/ani-rss) | AnimeMachine 镜像不嵌入 Ani-RSS 代码。Compose 可以启动其独立分发镜像，也可以通过带认证的连接器访问外部实例。AnimeMachine 对 Ani-RSS 媒体路径只做只读清点，不修改其中的媒体文件。 |
| Torrent Collector | AnimeMachine 自有的可选 Compose 服务，运行时访问用户网络环境可达的公共 Torrent 索引，只下载通过合集规则筛选的 `.torrent` 元数据，不下载媒体。索引站点的访问条件、内容权利和当地法律责任仍由部署者自行确认。 |
| [ASSRT](https://2.assrt.net/api/doc) | 可选的用户自定义字幕 API；凭据管理和已下载字幕的权利责任由用户承担。 |
| [OpenSubtitles](https://opensubtitles.stoplight.io/docs/opensubtitles-api) | 可选的用户自定义字幕 API，并受服务商配额和使用条款约束。 |
| [FFmpeg](https://ffmpeg.org/) | Docker 镜像安装 `ffprobe`，仅用于只读媒体流检查；本地部署可使用用户自行安装并配置的版本。 |
| [7-Zip](https://www.7-zip.org/) | Docker 镜像安装 7-Zip，用于安全解压支持的字幕压缩包；本地部署可使用用户自行安装并配置的版本。 |
| [truststore](https://github.com/sethmlarson/truststore) | 使用操作系统证书库完成经过验证的 HTTPS 连接（MIT）。 |
| [certifi](https://github.com/certifi/python-certifi) | 为经过验证的 HTTPS 提供可移植的 Mozilla CA 备用证书集（MPL-2.0）。 |

对于官方 Archive 资源，无论使用直连、已配置代理，还是由用户先通过浏览器下载 ZIP 再导入，AnimeMachine 都会按照官方描述文件给出的精确大小和 SHA-256 进行校验。TLS 校验不会被关闭；如果网络环境使用私有 HTTPS 中间 CA，可以通过 `ANM_CA_BUNDLE` 单独提供，不要求安装到整个操作系统。

正式发布前，需要完成以下检查：(1)复核全部容器镜像的许可证和 Notice；(2)固定不可变的镜像版本或 Digest；(3)生成 SBOM；(4)扫描凭据和私有路径；(5)针对计划分发的司法辖区和宣传表述完成必要的专业审查。

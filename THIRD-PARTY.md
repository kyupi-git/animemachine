# 第三方组件与数据边界

AnimeMachine 用于作品检索、收藏整理和下载管理。作品资料在运行时获取，媒体来自用户连接的数据源。发行包包含程序、所需组件及部分当季作品封面，不包含视频、Torrent 文件、磁力链接、私有目录或凭据。封面保留来源记录，不按 AnimeMachine 的代码许可证重新授权。

| 项目或数据源 | 集成边界 |
|---|---|
| [Bangumi Archive](https://github.com/bangumi/Archive) | 运行时下载并构建作品库，保留条目来源；发行包不预置派生数据库。当季封面单独从公开图片来源获取，经校验后随 Release 和镜像提供，来源记录在封面包清单及图片缓存中。 |
| [qBittorrent](https://github.com/qbittorrent/qBittorrent) | 独立分发的 GPL 下载客户端，AnimeMachine 只通过 Web API 管理用户明确批准的 AnimeMachine 任务；镜像中的相关声明和源码链接必须保留。 |
| [Ani-RSS](https://github.com/wushuo894/ani-rss) | 通过认证 API 管理订阅，或由 Compose 启动独立镜像；不嵌入其代码。媒体清点只读；删除订阅及文件的操作由用户确认后交给 Ani-RSS 执行。 |
| Torrent Collector | AnimeMachine 的可选采集服务，按作品和收录规则检索公共索引、保存 Torrent 元数据；媒体下载由下载器完成。 |
| [ASSRT](https://2.assrt.net/api/doc) | 可选字幕 API，使用用户配置的凭据检索字幕。 |
| [OpenSubtitles](https://opensubtitles.stoplight.io/docs/opensubtitles-api) | 可选的用户自定义字幕 API，并受服务商配额和使用条款约束。 |
| [FFmpeg](https://ffmpeg.org/) | Docker 镜像安装 `ffprobe`，仅用于只读媒体流检查；本地部署可使用用户自行安装并配置的版本。 |
| [7-Zip](https://www.7-zip.org/) | Docker 镜像安装 7-Zip，用于安全解压支持的字幕压缩包；本地部署可使用用户自行安装并配置的版本。 |
| [truststore](https://github.com/sethmlarson/truststore) | 使用操作系统证书库完成经过验证的 HTTPS 连接（MIT）。 |
| [certifi](https://github.com/certifi/python-certifi) | 为经过验证的 HTTPS 提供可移植的 Mozilla CA 备用证书集（MPL-2.0）。 |
| [OpenCC](https://github.com/BYVoid/OpenCC/tree/ver.1.1.9) | 仅使用 1.1.9 的 `TSCharacters` 和 `JPVariants` 字典生成离线姓名检索字形表，不嵌入转换库，不改写人物身份或原名。派生字形表省略有歧义的日文字形，来源地址、原文件 SHA-256 和完整 Apache-2.0 许可证保留在 `person-name-fold.json` 中。 |

对于官方 Archive 资源，无论使用直连、已配置代理，还是由用户先通过浏览器下载 ZIP 再导入，AnimeMachine 都会按照官方描述文件给出的精确大小和 SHA-256 进行校验。TLS 校验不会被关闭；如果网络环境使用私有 HTTPS 中间 CA，可以通过 `ANM_CA_BUNDLE` 单独提供，不要求安装到整个操作系统。

发布流程校验组件版本和校验值，生成 SBOM，并扫描凭据、私有路径及已知安全告警。

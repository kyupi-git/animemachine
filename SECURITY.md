# 安全策略

发现安全漏洞后，请通过 GitHub Security Advisories 私下报告。报告中不得包含真实 API Key、私有收藏库路径、Torrent 元数据或媒体清单。

AnimeMachine 对 HTTPS 启用 TLS 校验，对网络响应设置大小边界，对 Archive 校验文件大小和 SHA-256，并且只向用户明确配置的服务发送凭据。公共镜像只能接收公开 URL 或 ID。不得关闭证书校验；如果使用私有 CA，应通过 `ANM_CA_BUNDLE` 配置。

可选 Torrent Collector 只接收筛选、轮询和网络代理参数，不应获得 AnimeMachine、qBittorrent 或 Ani-RSS 的凭据。它写入的 `.torrent` 一律按不可信外部输入处理，后续仍须经过 AnimeMachine 的解析、身份核验和提交前计划检查。

当前只支持最新发布版本。任何凭据只要可能出现在日志或 Issue 中，都应先完成轮换，再共享相关内容。

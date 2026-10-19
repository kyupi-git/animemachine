# Compose

[中文部署指南](../../docs/guide.md#docker) · [English guide](../../docs/guide.en.md#docker) · [日本語ガイド](../../docs/guide.ja.md#docker)

下载 / Download / 保存 **[compose.yaml](compose.yaml)**，在文件所在目录运行 / then run / 同じディレクトリで実行：

```sh
docker compose up -d
```

打开 / Open / アクセス：`http://NAS-IP:8787`

| 配置 / Setup / 構成 | 服务 / Services / サービス |
| --- | --- |
| [默认 / Default / 既定](compose.yaml) | AnimeMachine + qBittorrent + Ani-RSS + Torrent Collector |
| [01](01-animemachine-standalone/compose.yaml) | AnimeMachine |
| [02](02-animemachine-external-qbt/compose.yaml) | AnimeMachine → existing qBittorrent |
| [03](03-animemachine-managed-qbt/compose.yaml) | AnimeMachine + qBittorrent + Torrent Collector |
| [04](04-full-stack/compose.yaml) | Same as default / 与默认相同 / 既定と同じ |

01–04 是替代方案，选择一个即可。它们不是安装步骤。`advanced/collector` 仅用于单独运行采集器。

01–04 are alternatives, not installation steps. Choose one. `advanced/collector` runs the collector separately.

01～04 は代替構成です。手順の番号ではありません。一つを選んでください。`advanced/collector` は収集サービスを単独で動かすための設定です。

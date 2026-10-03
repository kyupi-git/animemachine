[中文](reference.md) | [English](reference.en.md) | [日本語](reference.ja.md)<br>
[README](../README.ja.md) · [利用ガイド](guide.ja.md) · [アーキテクチャ](architecture.ja.md)

# 設定・技術リファレンス

このページでは、導入パラメーター、ネットワーク設定、データの保守、開発用の参照先を説明します。通常の設定は画面から行えます。操作手順は[利用ガイド](guide.ja.md)を参照してください。

<a id="environment"></a>

## 設定ファイルと環境変数

| 導入方法 | 設定の場所 |
| --- | --- |
| ローカルのリリース | 展開したフォルダーの `config.json`、`.env.local` |
| ソースから起動 | ルートの `config.json`、`.local/.env.local`。既定の状態保存先は `.local/state` |
| Docker Compose | `compose.yaml` と同じ場所の `.env`、マウントした `config/config.json` |

ランチャーが初期設定を作成します。通常の設定は画面から保存し、ポート、マウント、サービスアドレスを変える場合は環境ファイルを編集して再起動します。導入時に明示した値が画面の設定より優先されます。キーはサーバー側の認証情報ファイルに保存され、導入時のキーには Secret ファイルも使えます。

Compose の主な変数は次のとおりです。パス変数には**ホスト側のフォルダー**を指定します。コンテナ内のパスは Compose のマウントに従います。

| 変数 | 既定値 / 用途 |
| --- | --- |
| `ANM_IMAGE` | `ghcr.io/kyupi-git/animemachine:0.3.1` |
| `ANM_LIBRARY_DIR` | `./library`、コレクション |
| `ANM_TORRENT_POOL_DIR` | `./torrents`、Torrent プール |
| `ANM_EXTERNAL_LIBRARY_DIR` | `./external/read-only`、既存メディア |
| `ANM_ANI_RSS_MEDIA_DIR` | `./external/ani-rss`、Ani-RSS のメディア |
| `ANM_CONFIG_DIR`、`ANM_DATA_DIR` | `./config`、`./data`、設定と状態 |
| `ANM_IMPORTS_DIR` | `./imports`、手動で取得した Archive |
| `PUID`、`PGID` | `1000`。メディアへアクセスするユーザーとグループの ID |
| `ANM_SYNC_INTERVAL_MINUTES` | `30`、同期の間隔 |
| `ANM_ANI_RSS_URL`、`ANM_ANI_RSS_MODE` | Ani-RSS のアドレスと `prefer` / `fallback` / `manual` モード |
| `ANM_PUBLIC_URL` | 視聴機器から開ける AnimeMachine のアドレス |

設定例：[ローカル](https://github.com/kyupi-git/animemachine/blob/main/deploy/local/.env.local.example) · [単独 Compose](https://github.com/kyupi-git/animemachine/blob/main/deploy/compose/01-animemachine-standalone/.env.example) · [既存ダウンローダー](https://github.com/kyupi-git/animemachine/blob/main/deploy/compose/02-animemachine-external-qbt/.env.example) · [同梱ダウンローダー](https://github.com/kyupi-git/animemachine/blob/main/deploy/compose/03-animemachine-managed-qbt/.env.example) · [フル構成](https://github.com/kyupi-git/animemachine/blob/main/deploy/compose/04-full-stack/.env.example)。

<a id="ports"></a>

### アドレスとポート

ローカルのリリースは既定で `0.0.0.0:8787` を使用します。8788 に変える場合は `.env.local` に `ANM_WEB_PORT=8788` を記入し、再起動して `http://localhost:8788` を開きます。

Compose でも `ANM_WEB_PORT` でホスト側のポートを変えます。コンテナ内は 8787 です。`ANM_BIND_ADDRESS` はホスト側の公開アドレスを指定し、ローカル利用なら `127.0.0.1` にできます。アクセス範囲に応じてログインを設定し、初回アカウントを `data/state/auth/initial-admin.txt` に保存します。

<a id="service-access"></a>

### qBittorrent / Ani-RSS の管理画面

同梱 Compose では qBittorrent の Web UI がローカルの `8080`、Ani-RSS が `7789` です。NAS のアドレスから開く場合は `.env` に記入します。

```dotenv
QBT_BIND_ADDRESS=0.0.0.0
ANI_RSS_BIND_ADDRESS=0.0.0.0
```

`docker compose up -d` を実行し、`http://NASのアドレス:8080` または `http://NASのアドレス:7789` からログインします。qBittorrent の初回アカウントは `docker compose logs qbt-bootstrap` で確認できます。Ani-RSS の既定アカウントは `admin` / `admin` です。ログイン後、セキュリティ設定で変更できます。

これらはブラウザー用のアドレスです。Compose 内の接続では `http://qbittorrent:8080`、`http://ani-rss:7789` を使います。ホスト側のサービスへは `host.docker.internal` を使えます。Linux の独自 Compose では、対応する `host-gateway` を設定します。

## ネットワーク・プロキシ・証明書

作品データとカバーは接続状態に応じて公式ソース、ミラー、利用できるプロキシを選びます。経路とエラーは **設定 → 診断**で、ソースの設定は **設定 → 接続 → メタデータ通信・ミラー**で確認できます。

プロキシを使う場合は、環境ファイルに設定します。

```dotenv
HTTP_PROXY=http://host.docker.internal:7890
HTTPS_PROXY=http://host.docker.internal:7890
NO_PROXY=localhost,127.0.0.1,qbittorrent,ani-rss
```

ローカル起動では、実際のプロキシアドレスに置き換えます。`NO_PROXY` にはローカルサービスのアドレスを残し、LAN 内を直接接続します。

<a id="ANM_CA_BUNDLE"></a>

社内や独自の CA を使う場合は、`config/certs/custom-ca.pem` などに証明書を置き、Docker で `ANM_CA_BUNDLE=/Config/certs/custom-ca.pem` を設定します。ローカル起動では実際のパスを指定します。証明書の検証と Archive の公式 SHA-256 確認は、どの経路でも行います。

## 作品データ・収集・バックグラウンド処理

公式の `dump-*.zip` をインポートできます。設定画面で選ぶか、起動前に `imports` に入れます。初回、週次、手動の更新は同じ検証・統合の処理を使います。

定期確認は **UTC+8** の木曜日 02:12、新版がない場合の再確認は金曜日 02:12 です。日本時間ではそれぞれ 03:12 に当たります。その後は次週の確認に移り、進捗と最近の結果は状態データに保存します。

フル Compose 構成にはリソース収集サービスが含まれます。[単独の収集設定](https://github.com/kyupi-git/animemachine/blob/main/deploy/compose/torrent-collector.yaml)でも使えます。ソース、過去の検索、再試行、プロキシの設定は[詳細な環境変数の例](https://github.com/kyupi-git/animemachine/blob/main/deploy/compose/torrent-collector.advanced.env.example)を参照してください。収集サービスが Torrent プールに保存し、AnimeMachine が走査して、確定した計画からダウンロードを送信します。

## データファイルと保守

状態フォルダー内の主な保存先です。

| 場所 | 内容 |
| --- | --- |
| `catalog/anime-catalog.sqlite3` | 作品情報、作品名、人物、関係、検索インデックス |
| `catalog/runtime.sqlite3` | ローカルリソース、メディア、サービス同期の実行データ |
| `metadata/archive`、`metadata/cache` | Archive とキャッシュデータ |
| `auth` | アカウント、ログイン状態、初期認証情報 |
| `history` | ファイル変更の復元用バックアップ |

設定と状態のフォルダー全体を保存すれば、これらのデータも含めてバックアップできます。コピー前にサービスを停止し、一つの状態フォルダーは一つのインスタンスで使います。権限は用途に合わせ、ライブラリと状態は書き込み可能、外部メディアは読み取り可能、Torrent プールは収集サービスが書き込めるようにします。

## ソースからの起動と開発

Python 3.11 以降を使います。リポジトリをダウンロードし、OS に合う `scripts/windows/AnimeMachine.cmd`、`scripts/unix/AnimeMachine-Linux.sh`、`scripts/unix/AnimeMachine-macOS.command` を起動します。ランチャーが環境を作成し、依存パッケージをインストールします。

| コードの場所 | 担当 |
| --- | --- |
| [catalog](https://github.com/kyupi-git/animemachine/tree/main/src/animemachine/catalog) | Archive の取り込み、作品検索、関係、情報更新 |
| [torrents](https://github.com/kyupi-git/animemachine/tree/main/src/animemachine/torrents) | 識別、収集、完全性、候補の順位付け |
| [integrations](https://github.com/kyupi-git/animemachine/tree/main/src/animemachine/integrations) | qBittorrent、Ani-RSS、再生、字幕 |
| [library](https://github.com/kyupi-git/animemachine/tree/main/src/animemachine/library) | メディア走査、フォルダー計画、履歴の復元 |
| [web](https://github.com/kyupi-git/animemachine/tree/main/src/animemachine/web) | Web UI、翻訳、スタイル、操作 |
| [scripts](https://github.com/kyupi-git/animemachine/tree/main/scripts) | 検証、ビルド、リリース、ランチャー |

プロジェクトの確認には `python scripts/test_all.py`、品質確認には `python scripts/check_quality.py` を実行します。変更に参加する場合は[貢献ガイド](../CONTRIBUTING.md)を参照してください。

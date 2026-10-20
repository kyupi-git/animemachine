[中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)

# Docker Compose 導入ガイド

**`compose.yaml` は一つだけ選び、一度起動します。** 01～04 は構成の種類であり、順番に実行する作業ではありません。

## 1. Docker の準備

- **Windows / macOS：** [Docker Desktop](https://docs.docker.com/get-started/get-docker/) をインストールして起動します。
- **Linux / NAS：** Docker Engine と Docker Compose プラグインをインストールして起動します。NAS の「コンテナ管理」アプリも利用できます。[Docker 公式導入ガイド](https://docs.docker.com/compose/install/) を参照してください。
- 必要なバージョンは Docker Engine 24 以降、Docker Compose 2.20.3 以降です。ターミナルで確認します。

```sh
docker version
docker compose version
```

`Cannot connect to the Docker daemon` と表示されたら Docker サービスを起動します。`compose` が認識されない場合は Compose プラグインをインストールします。以下では、間にスペースがある `docker compose` を使用します。

## 2. 構成を一つ選ぶ

| 利用状況 | 設定ファイル | 起動するサービス |
| --- | --- | --- |
| 全機能を利用する場合（推奨） | [既定のフル構成](compose.yaml)（[04](templates/04-full-stack/compose.yaml) と同じ） | AnimeMachine、qBittorrent、Ani-RSS、Torrent Collector |
| 既存の動画を検索・再生するだけの場合 | [01 · 単独構成](templates/01-animemachine-standalone/compose.yaml) | AnimeMachine |
| 設定済みの qBittorrent を利用している場合 | [02 · 既存ダウンローダー接続](templates/02-animemachine-external-qbt/compose.yaml) | AnimeMachine と既存の qBittorrent |
| ダウンローダーが必要だが Ani-RSS は不要な場合 | [03 · ダウンローダー同梱](templates/03-animemachine-managed-qbt/compose.yaml) | AnimeMachine、qBittorrent、Torrent Collector |

Torrent Collector は `.torrent` メタデータを収集するサービスであり、動画自体はダウンロードしません。01/02 から既存の Ani-RSS に後で接続することもできます。[advanced/collector](advanced/collector/compose.yaml) では収集サービスを単独で実行できます。

## 3. ファイルの保存と起動

1. 設定やデータを保存し続ける場所に `AnimeMachine` などのフォルダーを作ります。**更新時も同じフォルダーを使います。**
2. 上の表から選んだ `compose.yaml` を開き、GitHub の **Download raw file** を押します。作成したフォルダーに、必ず `compose.yaml` という名前で保存してください（`compose.yaml.txt` は不可）。既定の構成では **`.env.example` のコピー、API キーの入力、初期化スクリプトの実行は不要**です。
3. そのフォルダーでターミナルを開きます。Windows ではフォルダーの空白部分を右クリックし、「ターミナルで開く」を選びます。Linux / macOS ならフォルダーでターミナルを開くか `cd` で移動します。NAS の Compose プロジェクト画面から YAML を取り込むこともできます。
4. 同じフォルダーで次を順に実行します。

```sh
docker compose config --quiet
docker compose up -d
docker compose ps
```

一つ目は設定の検証、二つ目はイメージ取得と起動、三つ目は状態確認です。初回のイメージ取得には時間がかかる場合があります。フル構成は通常 4 サービス、01 は 1 サービスです。取得に失敗したら Docker のネットワーク、DNS、レジストリへの接続を確認してください。

## 4. 初回ログイン

同じコンピューターからは `http://localhost:8787`、別の端末からは `http://NASのLAN内IP:8787` にアクセスします。例えば `http://192.168.1.20:8787` の IP は実際の値に置き換えてください。

初回起動で管理者アカウント（既定のユーザー名は `admin`）とランダムなパスワードが作成されます。次のコマンドで**初回起動のログ**を読み、`AnimeMachine login`、`Username`、`Password` を探してください。

```sh
docker compose logs animemachine
```

ランダムなパスワードがログに表示されるのは**生成した初回のみ**です。初回ログが残っていない場合、生成済みの初期認証情報はフォルダー内の `data/state/auth/initial-admin.txt`（コンテナー内は `/Data/state/auth/initial-admin.txt`）に保存されています。パスワードを変更した後は変更後の値を使います。更新や再起動でも既存アカウントは保持されます。

初回起動では今期の作品を先に準備し、残りの資料はバックグラウンドで補完します。作品一覧が表示されたら検索・購読・再生を利用できます。保存先フォルダーの作成とローカル Torrent ダウンロードは、全資料の準備後に利用できます。設定 → 接続で書き込み可能なライブラリ `/Library` と必要に応じて読み取り専用の `/External` を確認します。同梱 Ani-RSS は `/Media` を共有します。これらはコンテナー内のパスであり、ホストのパスとは異なります。

## 5. データの保存先

既定では、`compose.yaml` と同じ場所に次のフォルダーが作成されます。

| フォルダー | 内容 |
| --- | --- |
| `config` | アプリ・管理対象サービスの設定、認証情報 |
| `data` | データベース、アカウント、実行状態 |
| `library` | AnimeMachine が読み書きする動画ライブラリ |
| `imports` / `torrents` | 任意の取り込みフォルダー / `.torrent` プール（アプリからは読み取り専用） |
| `external/read-only` | 既存動画の読み取り専用マウント |
| `external/ani-rss` | 同梱 Ani-RSS とダウンローダーが共有する完成動画 |

既存動画は `external/read-only` に、書き込み可能なコレクションは `library` に配置します。別ディスクに置く場合は選んだ構成の `.env.example` を `compose.yaml` と同じ場所に `.env` としてコピーし、`ANM_LIBRARY_DIR=./library` を実際の**ホスト側パス**（Linux なら `/srv/anime` など）に変更して `docker compose up -d` を再実行します。フォルダーが存在し、指定した `PUID` / `PGID`（既定 1000）で書き込めることを確認してください。アプリ内のパスは引き続き `/Library` です。

02 を使う場合は、既存 qBittorrent の Web API にコンテナーから接続できることを先に確認します。`.env` に `ANM_MANAGED_QBITTORRENT_URL` と、ダウンローダーから見た**コンテナー内の媒体パス** `ANM_QBT_LIBRARY_DIR` を設定し、ホストとのマウント関係を照合してください。

## 6. 日常管理、更新、バックアップ

次のコマンドは必ず**元の Compose フォルダー**で実行します。

```sh
docker compose ps
docker compose logs --tail 100 animemachine
docker compose pull
docker compose up -d
```

最初の二つは状態と最新ログの確認、後の二つはイメージの取得とコンテナーの再作成です。既定イメージは `0.5.0` に固定されています。新しいバージョンに更新する場合は、先に `compose.yaml`（または `.env` の `ANM_IMAGE` など）のイメージタグを変更してください。`pull` だけでは固定バージョンは更新されません。

バックアップ前に `docker compose down` でサービスを停止します。このコマンドではデータは保持されます。`compose.yaml`、必要に応じて `.env`、`config`、`data`、`library`、動画や Torrent の保存先、および使用している収集サービスの名前付きボリュームをバックアップします。その後 `docker compose up -d` で再起動します。複数のインスタンスでは、それぞれ別の `data` フォルダーを使います。旧構成を切り替える場合、`docker compose up -d --remove-orphans` は不要な旧サービスを削除するため、カスタムサービスが含まれていないか確認してください。

## 7. よくある問題

- **8787 番ポートに接続できない：** `docker compose ps` と `docker compose logs --tail 100 animemachine` で状態を確認し、LAN 内 IP、ファイアウォール、ポート競合を調べます。必要なら `.env` に `ANM_WEB_PORT=8788` を指定し、`docker compose up -d` を再実行して `:8788` にアクセスします。
- **`permission denied`：** マウント先の権限と `PUID` / `PGID` を確認します。Linux ではターミナルのユーザーにも Docker の実行権限が必要です。コンテナーを実行するユーザーに必要な権限を付与します。
- **qBittorrent / Ani-RSS の管理画面に LAN からアクセスできない：** 既定のアドレスは `http://NASのIP:8080` と `http://NASのIP:7789` です。初期パスワードはそれぞれ `config/qbittorrent/.animemachine/web-password` と `config/ani-rss/.animemachine/web-password` に保存されます。更新時は既存のパスワードを引き継ぎます。[管理ポートの説明](../../docs/reference.ja.md#service-access)を参照してください。
- **別端末のプレーヤーが動画を開けない：** プレーヤーから到達可能なサービス URL を設定します。[接続とパスの説明](../../docs/guide.ja.md#connections)を参照してください。
- **コンテナーを誤って削除した：** マウント先のフォルダーと名前付きボリュームが残っていれば、同じフォルダーで `docker compose up -d` を実行できます。フォルダー自体の削除や `down -v` はデータ損失につながります。

機能の詳しい説明は[導入と使い方](../../docs/guide.ja.md)をご覧ください。

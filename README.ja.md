[中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)  
[README](README.ja.md) | [導入・利用ガイド](docs/guide.ja.md) | [アーキテクチャとデータベース](docs/architecture.ja.md) | [更新履歴](CHANGELOG.md)

# AnimeMachine · Automated Anime Library

AnimeMachine は全自動アニメライブラリシステムです。アニメのメタデータ、Torrent プール、メディアディレクトリ、外部の読み取り専用メディアライブラリを、一つのローカル Catalog と状態体系に整理します。**AnimeMachine 自体はアニメの検索やダウンロードを提供せず**、Torrent、マグネットリンク、メディアコンテンツも内蔵しません。ユーザーはローカルライブラリまたは外部読み取り専用ライブラリを設定し、自分の Torrent Pool を割り当てるか、Torrent Collector で公開インデックスから規則に合う一括リリース系 `.torrent` メタデータを収集し、qBittorrent や Ani-RSS を接続して実際のダウンロードや購読を行います。新作と所蔵が増え続ける環境で、リソースの出所、ディレクトリ命名、メディアファイル、作品情報、シリーズ関係、既存所蔵の整合性を維持し、大規模なアニメライブラリを継続的に管理します。

「リソースを見つける」段階から「コレクションに収める」までを分解すると、AnimeMachine は七つの処理を連続して行います。(1)原材料の選別では、Torrent プールを走査し、まず完全なリリースかどうかを判断したうえで、一括性、リリースグループ、ソース、解像度などを比較します。(2)投入では、ユーザーが確認したタスクを qBittorrent に渡すか、放送中作品の購読を Ani-RSS に渡します。(3)一次加工では、初回放送月、正式タイトル、シリーズ所属から多階層ディレクトリを計画します。(4)精加工では、前作、続編、総集編、番外編、派生、別解釈、シリーズ横断関係、キャラクター出演などの論理関係を構築します。(5)品質確認では、Torrent manifest とローカルファイルを項目単位で比較し、不足分だけを補い、信頼できる改訂証拠がある場合にだけ旧ファイルを一時保管して置換します。(6)パッケージングでは、中国語・英語・日本語の Web 画面からカタログ、候補、所蔵状態、シリーズ関係をまとめて確認できます。(7)保管では、ダウンロード済み、外部マッピング済み、ダウンロード待ちの作品を、今後も拡張・保守できるアニメリポジトリとして整理します。

![AnimeMachine ライブラリ概要](docs/images/library-overview.png)

*ライブラリ概要：最近の作品を既定で表示し、複数の条件で絞り込み・並べ替えできます。*

![AnimeMachine 作品詳細](docs/images/work-detail.png)

*作品詳細：Bangumi Archive から読み取った主要情報を表示します。*

![AnimeMachine プレイヤー引き渡し](docs/images/playback.png)

*プレイヤー引き渡し：全話を含む M3U プレイリストを生成し、選択話から開始できる状態でローカルプレイヤーへ渡します。VLC や PotPlayer など M3U 対応プレイヤーがあれば、OS や端末を問わず利用できます。ローカル保存がなくても Ani-RSS API からプレイリストを生成し、新作購読を依頼できます。*

![AnimeMachine 作品関係図](docs/images/relationship-graph.png)

*作品関係図：作品間の根拠から自動生成します。多対多・多層関係に対してノード配置と線路を調整し、確度の高い原データ異常を補正しながら、実用的な計算時間を維持します。*

## 主な機能

- Bangumi Archive を基礎にアニメカタログを構築します。初回起動時はローカル SQLite Catalog をバックグラウンドで作成し、完全性検査後に原子的に公開します。
- Torrent プールは増分走査します。指紋が変化していない処理済みファイルは再解析せず、大規模な全量走査中でも完了済みバッチから検索やリソース選択に利用できます。
- Docker の自動コレクション構成では Torrent Collector を有効化できます。タイトル、Torrent manifest、ローカル Catalog の証拠を組み合わせて `accept / reject / defer` を判定し、共有 Torrent プールへ書き込むのは `accept` だけです。証拠不足は固定の話数幅で完全性を推測せず `defer` のまま残します。毎週追加される単話リリースは Ani-RSS へ購読を依頼して遠隔アクセスできます。
- ダウンロード計画は、完全な一括リリース、話数/巻数の組み合わせ、差分補完、同一 infohash の既存タスクへのファイル選択追加に対応します。AnimeMachine 管理の qBittorrent タスクは先に計画を生成し、常に停止状態で送信します。計画内容を確認した後、ユーザーが qBittorrent 側で開始します。
- 管理対象ライブラリにはローカルメディアディレクトリまたは読み書き可能な UNC/NAS ディレクトリを利用できます。既存メディアは移動や改名をせず、外部読み取り専用ライブラリとしてマッピングできます。
- qBittorrent、Ani-RSS、Torrent Collector はすべて任意です。一部だけを使うことも、Compose で完全な自動コレクション経路を構成することもできます。
- 再生では M3U プレイリストを生成し、システムプレイヤー、VLC、PotPlayer に渡します。サーバーとクライアントでパスが異なる場合はプレイヤー側から見えるパスマッピングを設定でき、Ani-RSS メディアをマウントしていない場合でも HTTP 中継で Range シークと短時間切断からの再開に対応します。
- アーカイブ向けリソースでは、内蔵/外部字幕を確認し、ユーザーが設定した字幕サービスへ接続できます。字幕処理によって外部読み取り専用メディアへの書き込み権限が生じることはありません。
- 作品関係図でシリーズ内の位置と論理関係を表示し、関連作品へ直接移動できます。

## クイックスタート

### ローカル実行

Windows では `scripts/windows/AnimeMachine.cmd`、Linux では `scripts/unix/AnimeMachine-Linux.sh`、macOS では `scripts/unix/AnimeMachine-macOS.command` を実行します。ソースから初回起動する場合は、あらかじめ Python 3.11 以降が必要です。起動スクリプトが隔離環境を作成し、現在のプロジェクトをインストールします。

詳細は [導入・利用ガイド](docs/guide.ja.md#ローカル導入) を参照してください。Release には Windows、Linux、macOS の起動入口をすべて残していますが、直接同梱されるランタイムまたは依存関係はビルドしたプラットフォーム向けだけです。別の OS で実行する場合は Python 3.11 以降を用意し、初回起動時にそのプラットフォーム向けの互換依存関係を取得することがあります。

### Docker Compose

`deploy/compose` には四つの定義済み構成があります。(1)AnimeMachine 単体、(2)外部 qBittorrent 連携、(3)Torrent Collector と内蔵 qBittorrent、(4)Torrent Collector・qBittorrent・Ani-RSS を同じ Compose プロジェクトで管理する完全構成です。最初の三構成では外部 Ani-RSS を任意で接続でき、未設定でも Catalog、ディレクトリ、Torrent Pool、ローカルライブラリを管理できます。

0.2.0 の構成例は公開イメージ `ghcr.io/kyupi-git/animemachine:0.2.0` を既定で固定します。`latest` へ変更すると以後のリリースへ追従するため、運用環境では具体的なバージョンタグを固定したまま使うことを推奨します。

選択したディレクトリで `.env.example` を `.env` にコピーし、ホスト側のパスと必要なシークレットを設定してから `docker compose up -d` を実行します。四つの構成はコンポーネント境界によって区分されます。選択は、既存の qBittorrent、Ani-RSS、メディアディレクトリが別の場所で動作しているかどうかを基準にします。詳細は [導入・利用ガイド](docs/guide.ja.md#docker-compose) を参照してください。

## ドキュメント

- [導入・利用ガイド](docs/guide.ja.md)：ローカル/NAS/Compose 導入、初回カタログ構築、ダウンロード、Ani-RSS、再生、日常保守。
- [アーキテクチャとデータベース](docs/architecture.ja.md)：モジュール境界、データフロー、ディレクトリ規則、関係図アルゴリズム、SQLite エンティティ、状態原則。
- [サードパーティとデータ境界](THIRD-PARTY.md)；[セキュリティポリシー](SECURITY.md)；[コントリビューション](CONTRIBUTING.md)。

## ビルドとクリーンアップ

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

すべてのビルド成果物は `dist` に出力されます。クリーンアップスクリプトはビルド、テスト、インタープリタのキャッシュを削除し、データベース、カバーキャッシュ、ユーザー設定、コレクション履歴を保持します。用途は開発成果物の整理であり、AnimeMachine の初期化ではありません。

## データとライセンス

アニメの基礎カタログは [Bangumi Archive](https://github.com/bangumi/Archive) を使用し、Ani-RSS 連携は [ani-rss](https://github.com/wushuo894/ani-rss) を参照しています。サードパーティコンポーネント、データソース、ライセンス境界は [THIRD-PARTY.md](THIRD-PARTY.md) を参照してください。

AnimeMachine は AGPL-3.0-only で公開されています。

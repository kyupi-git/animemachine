[中文](CONTRIBUTING.md) | [English](CONTRIBUTING.en.md) | [日本語](CONTRIBUTING.ja.md)

# 開発への参加

AnimeMachine は Python 3.11 以降を使用します。画面は HTML・CSS・JavaScript で構成し、作品情報と実行状態は SQLite に保存します。

## 各コンポーネントの役割

| コンポーネント | 用途 |
| --- | --- |
| AnimeMachine | 作品の閲覧、メディアの照合、ダウンロード計画、視聴記録と再生リストの管理 |
| Bangumi Archive | 作品名、日付、制作スタッフと作品間の関係を提供 |
| qBittorrent | ダウンロードを実行して動画を保存。既存環境への接続と同時導入に対応 |
| Ani-RSS | リリース検索、購読管理、メディアとキャッシュ済み画像の提供。必要に応じて接続 |
| Torrent Collector | 作品別検索とダウンロード計画で使う Torrent メタデータの収集 |
| PotPlayer 拡張 | プレーヤーから正確な再生位置を送信。通常の再生は拡張なしでも利用可能 |

各コンポーネントは API または共有フォルダーで連携します。導入方法は[使用ガイド](docs/guide.ja.md)、データの流れと責務は[アーキテクチャ](docs/architecture.ja.md)を参照してください。

## ローカル開発

プロジェクトのルートで仮想環境を作成します。

```sh
python -m venv .venv
```

仮想環境を有効にして、開発用の依存関係を導入し、検証を実行します。

```sh
python -m pip install -e ".[test]"
python scripts/test_all.py
python scripts/check_quality.py
```

JavaScript の構文確認には Node.js を使います。配置設定を変更する場合は Docker Compose を導入し、テンプレートも検証します。

```text
node --check src/animemachine/web/static/app.js
python scripts/check_compose.py
```

## マニュアルの更新

`docs/manual` の原稿から VitePress でアプリ同梱のオフラインページを生成します。Node.js 22 以降をインストールし、次のコマンドを実行します。

```sh
npm --prefix docs/manual ci --ignore-scripts
npm --prefix docs/manual run build
```

本文や参照画像を変更したら、再ビルドして `src/animemachine/web/static/help` も変更に含めます。ビルド時に内部リンク、画像、配布用の構成テンプレートを確認し、テスト時に原稿との一致を検証します。利用者は画面の「マニュアル」から開くため、Node.js のインストールは不要です。

## ファイル構成

| ディレクトリ | 内容 |
| --- | --- |
| `src/animemachine` | アプリケーション、画面、組み込みリソース |
| `tests` | 合成テストデータ、単体テスト、結合テスト |
| `packaging` | Docker イメージと配布パッケージの定義 |
| `deploy/compose` | 標準のフル構成、代替テンプレート、単独収集サービスの設定 |
| `scripts` | テスト、検証、ビルド、保守の実行スクリプト |
| `docs` | 3 言語の使用ガイド、アーキテクチャ、設定リファレンス |

## 変更の提出

具体的な課題ごとに変更をまとめ、変更後の動作と検証結果を説明してください。作品照合、作品関係、データ取り込みの変更は、再現可能な例で確認します。保存データを変更する場合は、移行処理と旧バージョンからの更新テストを用意します。画面と使用説明は中国語・英語・日本語を合わせて更新します。

自動テストには合成メタデータと独立した一時フォルダーを使い、ダウンロードやプレーヤーとの連携は専用環境で確認します。提出物はソース、保守対象の文書、同梱マニュアル、再現用の例で構成し、個別の設定、認証情報、動画、配布パッケージはローカルで管理します。

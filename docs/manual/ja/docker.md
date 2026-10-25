# 2. Linux・Docker に導入

標準構成では、操作画面の AnimeMachine、購読用の Ani-RSS、ダウンロード用の qBittorrent、検索用の Torrent Collector が起動します。初回の接続とアカウント設定は自動で行われます。

## プロジェクト用フォルダーを作る

1. 使用する Linux に [Docker Engine](https://docs.docker.com/engine/install/) と [Compose プラグイン](https://docs.docker.com/compose/install/)を導入します。Docker Engine 24+、Compose 2.20.3+ を使用します。
2. AnimeMachine を動かす PC・サーバーでターミナルを開きます。
3. 動画を保存する空き容量のあるディスクに、固定のプロジェクトフォルダーを作成します。

```sh
mkdir -p ~/animemachine
cd ~/animemachine
```

このフォルダー内に `config`、`data`、`library`、`torrents`、`external` などが作成されます。動画は `library` または `external/ani-rss` に保存されるため、空き容量のあるディスクを選びます。

## Compose ファイルを取得する

[compose.yaml をダウンロード](../../../deploy/compose/compose.yaml)し、先ほどのフォルダーへ保存します。GitHub のファイル画面では **Download raw file** を選びます。ファイル名は **`compose.yaml`** のままにします。ターミナルからも取得できます。

```sh
curl -fL https://raw.githubusercontent.com/kyupi-git/animemachine/main/deploy/compose/compose.yaml -o compose.yaml
```

この一つのファイルで全サービスを起動できます。初回起動前に、各サービスのアカウントや設定ファイルを用意する必要はありません。

## ターミナルから起動する

`compose.yaml` を保存したフォルダーで実行します。

```sh
docker compose up -d
```

状態を確認します。

```sh
docker compose ps
```

**確認する状態：** 四つのサービスが実行中になります。Ani-RSS と Collector は AnimeMachine の準備を待って起動します。初回のコマンド実行中は、イメージの取得状況が表示されます。

## ページを開いてパスワードを確認する

1. 下記のコマンドで、初期アカウントとランダムパスワードを確認します。
2. 同じ PC では **<http://localhost:8787>** を開きます。別の端末では、サーバーの LAN アドレスを使います。例は `http://192.168.1.20:8787` です。
3. ログインし、[初回ログイン](first-use.md)へ進みます。

起動ログを確認します。

```sh
docker compose logs animemachine
```

| 管理画面 | LAN 内のアドレス |
| --- | --- |
| AnimeMachine | `http://NASのアドレス:8787` |
| Ani-RSS | `http://NASのアドレス:7789` |
| qBittorrent | `http://NASのアドレス:8080` |

日常の操作は AnimeMachine から行えます。他の管理画面を開く場合、ユーザー名は `admin`、初期パスワードはプロジェクト内の `config/ani-rss/.animemachine/web-password`、`config/qbittorrent/.animemachine/web-password` に保存されています。

## 既存サービスや別のディスクを使う

すでに Ani-RSS・qBittorrent がある場合は [01・02 テンプレート](https://github.com/kyupi-git/animemachine/tree/main/deploy/compose/templates)を選び、**設定 → 接続** から登録できます。標準ファイルと各テンプレートは代替構成なので、一つを選んで導入します。

別のディスクにある動画は[フォルダーの対応](collections.md#docker-folders)を設定します。フル構成では公開配信元への接続を確認し、必要に応じて利用可能な代替先を選びます。

## この手順で困ったら

- **設定ファイルが見つからない：** `compose.yaml` のある場所でコマンドを実行し、名前が `compose.yaml.txt` になっていないか確認します。
- **イメージ取得に失敗：** Docker のエラーを確認し、接続復旧後に同じコマンドを再実行します。取得済みのレイヤーは再利用されます。
- **Docker の権限エラー：** Docker を操作できるアカウントを使用します。管理者権限が必要な構成では、Docker のコマンドに `sudo` を付けます。
- **ポートが使用中：** 同じ場所の `.env` に `ANM_WEB_PORT=8788` などの空きポートを指定して再起動します。ブラウザー側も 8788 を使用します。
- **一時停止したい：** `docker compose stop` を実行します。設定・記録・動画は保存されたままです。

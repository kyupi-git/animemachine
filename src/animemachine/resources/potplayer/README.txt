AnimeMachine / PotPlayer 64-bit

中文
1. 退出 PotPlayer，将压缩包的 Extension 文件夹合并到 PotPlayer 安装目录。
2. 重新打开播放器，扩展自动生效，无需在网页勾选。
3. 通过 AnimeMachine 打开作品或续看列表。记录实际播放位置，下次从停止位置前五秒开始。
扩展只在播放器内运行，不写动画库，不读取播放历史。暂停、切集和退出时同步位置；断线或强制结束时使用最近已保存的位置。
已验证 PotPlayer 260623（64 位）。需要保留默认标题中的 [ANM:…] 标记。
卸载扩展后，继续使用媒体访问记录。
未安装扩展时，列表仍可正常播放，按集数记录观看情况。
卸载：退出播放器，删除 Media/PlayParse 中的两个 AnimeMachine 文件及本源码。

English
1. Close PotPlayer. Merge this archive's Extension folder into the PotPlayer installation folder.
2. Restart the player. The extension works automatically.
3. Open a title or catch-up playlist through AnimeMachine. Records the playback position and resumes five seconds earlier next time.
The extension runs inside the player, writes no library files and reads no playback history.
Verified with 64-bit PotPlayer 260623. Keep the [ANM:…] marker in the player title.
After uninstalling the extension, media-access tracking continues to work.
Without the extension, playlists still play normally and record viewing by episode.
To uninstall, close the player and remove the two AnimeMachine extension files and this source.

日本語
1. PotPlayer を終了し、ZIP の Extension フォルダーをインストール先に統合します。
2. プレーヤーを再起動すると自動で有効になります。
3. AnimeMachine から作品またはまとめ再生リストを開きます。再生位置を記録し、次回は停止位置の五秒前から再開します。
プレーヤー内でのみ動作し、ライブラリへの書き込みや再生履歴の読み取りは行いません。
64 ビット版 PotPlayer 260623 で検証済みです。タイトルの [ANM:…] を残してください。
拡張を削除した後も、アクセスによる記録を利用できます。
拡張がなくてもリストは通常どおり再生でき、視聴した話数を記録します。
削除する場合は終了後、Media/PlayParse 内の AnimeMachine 拡張ファイルとソースを削除します。

Source: anm_playback.c / MediaPlayParse - AnimeMachine.as
License: AGPL-3.0-only, https://github.com/kyupi-git/animemachine
Build: Zig 0.14.1, x86_64-windows-gnu; see the command in anm_playback.c.

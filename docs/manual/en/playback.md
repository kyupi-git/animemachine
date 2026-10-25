# 5. Start watching

Once media is accessible, choose an episode and a player in title details. The player runs on the computer or phone you are using; AnimeMachine can run on the NAS.

## Play an episode on your PC

1. Open title details and find **Playback**.
2. If several sources or DVD/BD editions exist, select the source and edition.
3. Choose the starting episode and select **Open in PotPlayer** or **Open in VLC**.
4. Accept the browser's request to open the external app.

![Playback: choose a source, edition, episode, and player](../../images/work-detail.en.png)

**Expected result:** the player starts the selected episode. To play several titles consecutively, use [Continue watching](continue-watching.md).

## Save precise positions with PotPlayer

1. Choose **Install progress extension** to download the extension package.
2. Close PotPlayer. Follow the package's `README.txt` to merge its `Extension` folder into the PotPlayer installation folder.
3. Restart PotPlayer and begin playback through AnimeMachine.

The extension saves precise positions and resumes five seconds before the last stop. Without it, playback still works and media access records episodes. Records belong to the account that generated the playlist.

## Repeated browser launch prompts

In Edge or Chrome on Windows, choose **Copy permission command**, press `Win + R`, enter `cmd`, press Enter, paste and run the command, then reopen the browser.

![Player controls: permission command and progress extension](../../images/playback.en.png)

The command allows this site to open PotPlayer and VLC for your Windows account, preserving existing permissions. If copying is blocked, a dialog selects the complete command; use `Ctrl+C`, then paste it into `cmd`.

## Phones, copied links, and downloaded playlists

Install VLC on the phone, then choose **Open in VLC** in its browser. You can also copy the playlist address into the player's network-stream field, or download and open the M3U file.

These methods retain the account associated when the playlist was generated. Media requests indicate an episode was accessed; **ordinary VLC media requests cannot report an exact stopping position**. Prefetching can also access an episode early. [Correct the record](continue-watching.md#correct) when needed.

A playlist contains the episodes available when it was generated. Generate another list after new episodes arrive.

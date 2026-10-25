# 10. Troubleshooting

Start with the symptom you see. Retry the original action after each check, so that each change has a clear result.

## The page will not open {#open-page}

1. Keep the desktop startup window running, or check that the Docker container is running.
2. Use the address and port shown in its startup log.
3. On a phone, replace `localhost` with the runtime device's LAN address.
4. If a port is occupied, select another one using the [deployment steps](docker.md), then restart.

## I cannot find the password

Check the first startup window or container log. The desktop package saves the initial account in `data/state/auth/initial-admin.txt`; the default Docker stack uses the same path under its project folder. If you changed the password, use that account; another administrator can assist with account management.

## Titles or covers have not appeared

The first start prepares recent titles before the complete catalog and remaining images. Check the home-page progress and **Settings → Diagnostics**. Downloads retry after connectivity recovers. Search older titles again once the full catalog is ready.

## A resource was found but nothing is downloading {#download}

1. Check that you selected a release and confirmed its subscription or download plan.
2. Check Ani-RSS / qBittorrent in **Settings → Connections**.
3. In the downloader, check whether the task exists, its destination is available, and peers can be reached.
4. If Torrent results are collapsed, expand them before selecting a group and variant.

## Existing files cannot be played

1. Confirm the media folder is readable in **Settings → Connections**.
2. In Docker, mount the NAS folder first, then enter its container path.
3. Let the inventory finish and select the source that contains the media.
4. Check main-episode classification and title/episode names. See [Existing collections](collections.md).

## The player does not open or reports a bad address

1. Install the player on your current device and allow the browser to open external apps.
2. Copy the playlist into the player, or download the playlist file.
3. Use an AnimeMachine address reachable from the player; `localhost` from another device points to that device.
4. Generate a fresh playlist if the old one expired or new episodes arrived.

The PotPlayer extension saves precise positions on Windows. VLC and ordinary media access record episodes; use [viewing records](continue-watching.md#correct) to correct a mistaken inference.

## Continue watching is empty

Check that the season contains subscribed TV titles whose latest episode is unfinished and whose media is playable. Paused, unsubscribed, hidden, and completed-latest titles are excluded from a new playlist.

## Which log messages need attention?

In **Settings → Logs**, filter to warnings and errors around the time of the action. For a persistent issue, note the app version, deployment method, steps, and relevant error. Remove passwords, keys, and personal paths before sharing logs or screenshots.

# 4. Subscribe and download

Use the featured Witch title to choose a release and download an episode. Ani-RSS subscriptions follow later episodes automatically; Torrent downloads submit the release you select now.

## Find the title

1. Enter a title in **Title / alias**, or open **Release radar**.
2. In the radar, select its season and click the title to open details.
3. Check the **Ani-RSS** results; use resource search if you need fresh results.

![Release radar: seasonal titles and episode updates](../../images/release-radar.en.png)

**Sync** retrieves subscription and media status already known to Ani-RSS. **Search** checks the season's titles one by one. The search runs in the background with progress shown on the page.

## Choose an Ani-RSS release

1. Choose a release group.
2. If it offers several variants, use the dropdown to check subtitles, platform, and quality, such as simplified/traditional Chinese or Baha.
3. Select the release and choose **Enable subscription**. Check the group and variant on the confirmation page, then choose **Enable subscription** again.
4. Wait for **Subscribed**. Downloaded episodes appear in the title's playback area.

![Choose a group and its specific release variant](../../images/resource-variants.en.png)

**Expected result:** the title is subscribed and available media appears in Playback. Ani-RSS handles later episodes while AnimeMachine synchronizes the status.

Language or quality mismatch labels refer to your current preferences. If you want that variant anyway, choose the override action and review the mismatch before confirming.

## Track a future title or archive release

Open **Track releases** in title details and choose **Subscribe when available**. The saved intent appears as **Awaiting releases**. Background checks enable an Ani-RSS subscription when a matching release becomes available. Your choice is kept while Ani-RSS is disconnected.

Tracking uses the release groups in Settings by default. To narrow the selection, clear that option and expand the group, for example **Kirara Fantasia → Baha** only. Existing Ani-RSS subscriptions keep their settings.

For BDRip and other collection editions, choose **Track archive releases**. Matching releases produce a notification; you choose whether to download. Open **Track releases** in Release radar to review, change, or stop tracking.

## Choose a Torrent release

1. Confirm qBittorrent is connected in **Settings → Connections**.
2. Choose **Search torrents** in title details and wait for results.
3. Expand **Torrent**, choose a group, then choose its subtitle and quality variant.
4. Choose **Preview download plan**, check the title, files, and destination, then submit.
5. Open qBittorrent, select the newly added stopped task, and start it.

![Expand Torrent results and select a group and variant](../../images/manual/torrent-results.en.png)

Local Torrent plans are added stopped; starting the task begins the transfer. Ongoing series are checked for season and episode continuity. Completed titles and films remain available for collection downloads.

For ongoing series with hundreds of episodes, each variant needs a continuous recent 24-episode window and may trail the latest release by one episode. It does not need the entire history in one search. Archive editions remain available for collecting older episodes in batches.

## Use your own Torrent pool

In **Settings → Connections → Torrent Collector**, title searches and the pool can work independently:

| Workflow | Configuration |
| --- | --- |
| Find a release for one title | Choose **Search Torrents** in its details; no separate collector service is needed |
| Keep your own folder of `.torrent` files | Set **Torrent pool path** and enable **Scan the pool on a schedule** |
| Add Torrents to the pool automatically | Deploy Torrent Collector with Compose and enable **Collect Torrents automatically** |

For an existing pool, you can turn off automatic collection while keeping scheduled scans. All three workflows use the same release preferences and title-detail selection. Review the file list and destination before submitting a download.

## A download has not started

Check connection and task status in AnimeMachine, then inspect the task and available seeds in qBittorrent. A submitted task waiting for peers differs from a download not yet submitted. Follow the [download checks](troubleshooting.md#download) in order.

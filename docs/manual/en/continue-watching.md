# 6. Continue watching

Continue watching combines subscribed TV titles with new episodes left to watch. Each title starts at its saved resume episode, includes that episode, and continues to the latest playable one.

## Build this viewing session

1. Open **Release radar** and choose a season.
2. Select **Continue watching** and wait for the episode list.
3. Check the titles you want. None are checked initially; selecting at least one enables the player buttons.
4. Drag each row by its handle to change the order. Adjust the episode range if needed.
5. Choose a player. The final playlist includes checked titles only, from top to bottom.

![Continue watching: select titles and drag them into order](../../images/continue-watching.en.png)

**Expected result:** the player's playlist contains the selected titles and episodes in your chosen order.

![PotPlayer sidebar: multiple titles in one playlist](../../images/potplayer-weekly.en.png)

For example, A resumes at episode 3 and has episodes through 4; B resumes at 4 and has episodes through 5. With A first, the list contains A 3–4, then B 4–5. The next session is recalculated from the viewing records actually received.

## Understand the pale-green highlight

Pale green means **subscribed, with the latest episode not yet finished**. Opening details or changing the resume episode to the latest episode keeps this highlight.

The latest episode is considered finished when any one of these applies:

- You choose **Latest episode finished**.
- You save a resume episode greater than the current latest episode.
- The PotPlayer extension reports at least **90%** progress through the latest episode.

A new episode starts a new check. Paused, unsubscribed, and hidden titles are excluded from newly generated catch-up playlists; their viewing records remain saved.

## Correct a viewing record {#correct}

1. Click the number in the radar's **Watched** column, or open the viewing record in title details.
2. Enter the episode to start from next time and, if needed, a precise position.
3. Choose **Save**. The radar and future playlists use the updated record.

![Viewing record: save, restart, or mark the latest episode finished](../../images/viewing-record.en.png)

**Watch from the beginning** resets the resume state. **Latest episode finished** explicitly confirms completion. Use **Undo** in the same window to restore the preceding record after a mistake.

Generating a playlist does not mark all its episodes watched. Media-access inference can be affected by player prefetching; precise extension feedback is more reliable.

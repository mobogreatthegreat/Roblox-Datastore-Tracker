# Datastore Monitor

Terminal dashboard that tracks the health of your own Roblox game's DataStore requests (Healthy / Unhealthy / Critical / Insufficient). It polls the Creator Hub's internal `analytics-query-gateway` endpoint (not Open Cloud), classifies every minute bucket from the `DataStoreRequestsByStatus` breakdown, and renders a colorized glyph histogram in the terminal.

Authentication uses your own `.ROBLOSECURITY` cookie, so the dashboard only ever shows universes your account already has access to.

## Features

- Live glyph histogram with one column per bucket, oldest on the left and newest on the right.
- Rolling window: every poll the histogram slides forward one bucket, appends the new value and hides the oldest, so already-rendered minutes are never rewritten by late-arriving API data.
- Five-state classification per bucket with a configurable failure-ratio threshold for Critical.
- Countdown bar to the next fetch, so a frozen screen is never mistaken for a healthy game.
- Per-game sections with status counts, earliest/latest timestamps, and multiple games in one dashboard.
- Graceful degradation: an unreachable or unauthorized query renders an error line for that game only, leaving the rest of the dashboard live.
- Raw payloads are written to `output.json` and appended to `output_history.json` (capped at the last 500 fetches) for later inspection.
- `--once` renders a single snapshot and exits, for use in CI or a cron job.

## Requirements

- Python 3.9 or newer.
- Python packages:

  ```
  pip install -r requirements.txt
  ```

- A Roblox account with Creator Dashboard access to the universe you want to monitor. The query is rejected without it.
- No Open Cloud API key is needed, and none is used.

## Quick start

1. Put your cookie in `.env` as a single line:

   ```
   Cookie="YOURCOOKIE"
   ```

2. Copy the example config and set the `Name` and `UniverseId` of the game to track:

   ```json
   "Games": [
       {
           "Name": "My Game",
           "UniverseId": 1234567890
       }
   ]
   ```

3. Run the dashboard:

   ```
   python datastore_monitor.py
   ```

   Use `--once` for a single fetch and exit, and `--config path/to/config.json` to load a different config.

> [!NOTE]
> Make sure to rename the config file to anything other than `config.example.json` (`config.json` is recommended).

## Configuration

`config.json` is read from the repository root and is gitignored, since it holds your own universe IDs. Copy `config.example.json` to get started.

All options live in `config.json`:

- **Endpoint**: analytics query gateway URL. The `{UniverseId}` placeholder is filled in per game, so this rarely needs changing.
- **Metric**: defaults to `DataStoreRequestsByStatus`.
- **Granularity**: `OneMinute`, `HalfHour`, `OneDay` or `OneWeek`.
- **Breakdown**: dimension list, normally `["DatastoreStatus"]`. An empty list disables the breakdown.
- **HistoryHours** / **HistoryDays**: size of the fetched window. `HistoryHours` takes precedence when both are set.
- **PollSeconds**: delay between fetches.
- **CriticalThreshold**: failure ratio above which a bucket is Critical (0.1 = 10%).
- **MaxPoints**: maximum histogram width. Wider windows are downsampled by summing consecutive buckets.
- **Games**: list of `{ "Name", "UniverseId" }` objects.

To watch several games at once, add more entries to `Games`. Each one gets its own section, fetched and classified independently.

## Reading the histogram

| Glyph | Meaning | Cause |
| --- | --- | --- |
| `0` | Healthy | Requests succeeded. |
| `+` | Unhealthy | Some requests failed, but the ratio stayed under `CriticalThreshold`. |
| `!` | Critical | Failures exceeded `CriticalThreshold`. |
| `\` | Insufficient | Not enough traffic in the bucket to be meaningful, or Roblox reported `N/A`. |
| `?` | Unknown | The API returned no point for that minute, or a status label that is not recognized. |

The `v` marker sits above the newest column with its timestamp, and `^` below the oldest.

> [!TIP]
> Run with `--once` first. If the dashboard prints an auth error, the cookie is expired or the account lacks Creator Dashboard access to that universe, and no amount of polling will fix it.

> [!NOTE]
> A column of `?` glyphs across the right edge normally means the API has not published the most recent minutes yet. It is not a failure signal.

## Status classification

Roblox returns a breakdown label per bucket, and each label is mapped to one of the five states:

- **Healthy**: an explicit allow-list of success labels such as `Ok` and `200 OK`.
- **Insufficient**: `N/A`, `No Data`, `Insufficient` and similar, meaning the bucket is not statistically meaningful.
- **Unhealthy**: a large allow-list of error and throttle codes, plus a keyword fallback for anything containing `fail`, `throttl`, `invalid`, `forbidden` and similar.
- **Unknown**: any label that matches none of the above, and any minute the API skipped.

Buckets are then resolved into a single state per minute: a gap or an unrecognized label becomes `?`, a bucket with no traffic becomes `\`, and otherwise the failure ratio decides between `0`, `+` and `!`.

> [!IMPORTANT]
> An unrecognized status label is deliberately reported as `Unknown` rather than assumed healthy. A new Roblox error code shows up as `?` instead of silently improving the numbers.

## Files

- `output.json`: the most recent raw payload per game.
- `output_history.json`: the last 500 payloads, appended on every successful fetch.

Both are regenerated at runtime and can be deleted safely.

## Troubleshooting

- **No cookie found**: `.env` is missing, empty, or does not define `Cookie`. `ROBLOSECURITY` is accepted as an alternative name.
- **Config not found**: `config.json` does not exist yet. Copy `config.example.json` and edit it.
- **Auth error or empty response**: the cookie expired, or the account has no Creator Dashboard access to that universe. Cookies are long-lived but not permanent; re-copy it from the browser when this happens.
- **Everything shows as `?`**: the game generated no DataStore traffic in the window, or the universe ID is wrong. Universe IDs are not place IDs; the universe ID is in the Creator Dashboard URL.
- **Everything shows as `\`**: requests happened but Roblox suppressed the values as not statistically significant. This is normal for low-traffic games.
- **Dashboard scrolls but the newest column stays `?`**: the current minute is usually incomplete. The following poll fills it in.
- **Histogram is narrower than the window**: `MaxPoints` downsamples by summing buckets. Raise it, or widen the terminal, to see more detail.

## License

See repository for license details.
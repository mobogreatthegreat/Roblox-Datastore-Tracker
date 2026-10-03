import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.text import Text

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
ENV_PATH = SCRIPT_DIR / ".env"
OUTPUT_PATH = SCRIPT_DIR / "output.json"
HISTORY_PATH = SCRIPT_DIR / "output_history.json"

STATUS_GLYPH = {
    "Healthy": "0",
    "Unhealthy": "+",
    "Critical": "!",
    "Insufficient": "\\",
    "Unknown": "?",
}

STATUS_STYLE = {
    "Healthy": "bold green",
    "Unhealthy": "bold yellow",
    "Critical": "bold red",
    "Insufficient": "dim white",
    "Unknown": "bold magenta",
}

SEVERITY_RANK = {"Healthy": 0, "Insufficient": 1, "Unknown": 2, "Unhealthy": 3, "Critical": 4}

GLYPH_RANK = {STATUS_GLYPH[name]: SEVERITY_RANK[name] for name in STATUS_GLYPH}

FAILURE_CATEGORIES = ("Unhealthy", "Critical")

# Wire enum values (verified): 2=OneMinute, 3=HalfHour, 4=OneDay, 5=OneWeek
GRANULARITY_ENUM = {"OneMinute": 2, "HalfHour": 3, "OneDay": 4, "OneWeek": 5}

GRANULARITY_MINUTES = {
    "OneMinute": 1, "HalfHour": 30, "OneDay": 1440, "OneWeek": 10080,
}

HEALTHY_LABELS = {
    "ok", "200", "200ok", "success", "succeeded", "healthy",
}

INSUFFICIENT_LABELS = {
    "na", "none", "nodata", "notmeaningful",
    "notstatisticallysignificant", "insufficient",
}

FAILURE_LABELS = {
    "accessforbidden", "attributeformaterror", "cantstorevalue", "datastoredeleted",
    "datastorethrottled", "getasyncthrottle", "getsortedthrottle",
    "getversionasyncthrottle", "getversionattimeasyncthrottle", "increasyncthrottle",
    "internalerror", "internalservererror", "invalidexclusivestartkey", "invalidkeyname",
    "invalidobjectkey", "invalidplace", "invalidtarget", "invalidtimestamp",
    "invaliduniverse", "invalidversion", "keynameempty", "keynamelimit", "keynotfound",
    "keythrottled", "listdatastoresasyncthrottle", "listkeysasyncthrottle",
    "listversionsasyncthrottle", "maxvalueinvalid", "minmaxorderinvalid",
    "minvalueinvalid", "operationnotallowed",
    "orderedlistgameserverthrottled", "orderedreadgameserverthrottled",
    "orderedremovegameserverthrottled", "orderedwritegameserverthrottled",
    "orderedlistexperiencethrottled", "orderedreadexperiencethrottled",
    "orderedremoveexperiencethrottled", "orderedwriteexperiencethrottled",
    "pagesizegreater", "pagesizelesser", "parameternotallowed", "removeasyncthrottle",
    "serviceunavailable", "setasyncthrottle", "standardlistgameserverthrottled",
    "standardreadgameserverthrottled", "standardremovegameserverthrottled",
    "standardwritegameserverthrottled", "standardlistexperiencethrottled",
    "standardreadexperiencethrottled", "standardremoveexperiencethrottled",
    "standardwriteexperiencethrottled", "studioaccesstoapisnotallowed",
    "toomanyrequests", "transformthrottle", "updateasyncthrottle",
    "useridlimitexceeded", "valuenotallowed", "valuenotnumeric", "valuetoolarge",
}

FAILURE_KEYWORDS = (
    "fail", "error", "throttl", "denied", "forbidden", "notfound",
    "invalid", "timeout", "unavailable", "toomany", "exceeded", "deleted",
    "limit", "empty", "larger", "lesser", "cannot", "cant", "mismatch",
    "notallowed", "nonnumeric", "transform", "keyname", "valuetoo",
)

VALUE_PATHS = (
    ("operation", "queryResult", "values"),
    ("operation", "response", "values"),
    ("operation", "values"),
    ("queryResult", "values"),
    ("response", "values"),
    ("values",),
)

HISTORY_LIMIT = 500

CONSOLE = Console()

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)

def compute_window(config: dict) -> tuple:
    end_time = datetime.now(timezone.utc)
    if config.get("HistoryHours"):
        start_time = end_time - timedelta(hours=float(config["HistoryHours"]))
    else:
        start_time = end_time - timedelta(days=config.get("HistoryDays", 7))
    return start_time, end_time

def granularity_minutes(config: dict) -> int:
    return GRANULARITY_MINUTES.get(config.get("Granularity", "OneMinute"), 1)

# Contiguous window of buckets a complete dataset must fill. Missing buckets
# (no point returned by the API) are treated as Unknown.
def expected_timestamps(config: dict) -> list:
    granule = granularity_minutes(config)
    if config.get("HistoryHours"):
        total_minutes = int(float(config["HistoryHours"]) * 60 / granule)
    else:
        total_minutes = int(float(config.get("HistoryDays", 7)) * 1440 / granule)
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=(total_minutes - 1) * granule)
    step = timedelta(minutes=granule)
    timestamps = []
    current = start
    for _ in range(total_minutes):
        timestamps.append(current.strftime("%Y-%m-%dT%H:%M:%SZ"))
        current += step
    return timestamps

def build_query(config: dict, universe_id) -> dict:
    start_time, end_time = compute_window(config)
    granularity_name = config.get("Granularity", "OneMinute")
    query = {
        "resourceType": "RESOURCE_TYPE_UNIVERSE",
        "resourceId": str(universe_id),
        "metric": config.get("Metric", "DatastoreRequestsByStatus"),
        "granularity": GRANULARITY_ENUM.get(granularity_name, 2),
        "startTime": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    breakdown = config.get("Breakdown", [{"dimensions": ["DatastoreStatus"]}])
    if breakdown:
        query["breakdown"] = breakdown
    return {
        "resourceType": "RESOURCE_TYPE_UNIVERSE",
        "resourceId": str(universe_id),
        "query": query,
    }

# Walks several possible JSON shapes to find the list of breakdown series.
def extract_values(data: dict) -> list:
    for path_keys in VALUE_PATHS:
        current = data
        for key in path_keys:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if isinstance(current, list):
            return current
    return []

class RobloxClient:
    def __init__(self, cookie: str):
        self.cookie = cookie
        self.csrf_token = None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://create.roblox.com",
            "Referer": "https://create.roblox.com/",
        })
        self.session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")

    # POST to /v2/logout without a token gives a 403 whose header carries one.
    def harvest_csrf_token(self):
        try:
            response = self.session.post("https://auth.roblox.com/v2/logout", json={}, timeout=20)
            if response.status_code == 403:
                token = response.headers.get("x-csrf-token")
                if token:
                    return token
        except requests.RequestException:
            pass
        try:
            response = self.session.get("https://create.roblox.com/dashboard/creations", timeout=25)
            match = re.search(r'name="csrf-token"\s+content="([^"]+)"', response.text)
            if not match:
                match = re.search(r'["\']CsrfToken["\']\s*:\s*["\']([^"\']+)', response.text)
            if match:
                return match.group(1)
        except requests.RequestException:
            pass
        return None

    def ensure_csrf(self):
        if not self.csrf_token:
            self.csrf_token = self.harvest_csrf_token()

    def post_json(self, url: str, payload: dict) -> requests.Response:
        self.ensure_csrf()
        headers = {"x-csrf-token": self.csrf_token} if self.csrf_token else {}
        response = self.session.post(url, json=payload, headers=headers, timeout=60)
        if response.status_code == 403:
            token = response.headers.get("x-csrf-token")
            if token:
                self.csrf_token = token
            else:
                self.csrf_token = None
                self.ensure_csrf()
            headers = {"x-csrf-token": self.csrf_token} if self.csrf_token else {}
            response = self.session.post(url, json=payload, headers=headers, timeout=60)
        return response

    def get_json(self, url: str) -> requests.Response:
        headers = {"x-csrf-token": self.csrf_token} if self.csrf_token else {}
        return self.session.get(url, headers=headers, timeout=60)

def error_message(data, fallback: str) -> str:
    if isinstance(data, dict):
        operation = data.get("operation") or {}
        error = operation.get("error") or data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return error["message"]
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict) and first.get("message"):
                return first["message"]
        if isinstance(error, str):
            return error
    return fallback

# Long-running queries are polled through the returned operation path until done.
def fetch_analytics(client: RobloxClient, config: dict, universe_id) -> list:
    body = build_query(config, universe_id)
    endpoint = config["Endpoint"].format(UniverseId=universe_id)
    response = client.post_json(endpoint, body)

    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code != 200:
        raise RuntimeError(error_message(data, f"HTTP {response.status_code}"))
    if not isinstance(data, dict):
        raise RuntimeError("Malformed analytics response")

    operation = data.get("operation", data)
    attempts = 0
    while not operation.get("done", True) and operation.get("path") and attempts < 60:
        attempts += 1
        operation_id = operation["path"]
        poll_url = f"https://apis.roblox.com/analytics-query-gateway/v1/operations/{operation_id}?resourceType=RESOURCE_TYPE_UNIVERSE&resourceId={universe_id}"
        try:
            response = client.get_json(poll_url)
            if response.status_code == 200:
                data = response.json()
                operation = data.get("operation", data)
        except requests.RequestException:
            pass
        time.sleep(1)

    if operation.get("error"):
        raise RuntimeError(f"Query error: {operation['error'].get('message', operation['error'])}")
    return extract_values(data)

def normalize_label(label) -> str:
    return re.sub(r"[^a-z0-9]", "", str(label).lower())

def classify_label(label) -> str:
    text = normalize_label(label)
    if not text:
        return "Unknown"
    if text in HEALTHY_LABELS:
        return "Healthy"
    if text in INSUFFICIENT_LABELS or "insufficient" in text:
        return "Insufficient"
    if text in FAILURE_LABELS:
        return "Unhealthy"
    if any(word in text for word in FAILURE_KEYWORDS):
        return "Unhealthy"
    # Anything unrecognized is reported as Unknown rather than assumed healthy.
    return "Unknown"

def breakdown_value(entry: dict):
    breakdown = entry.get("breakdownValue") or []
    if isinstance(breakdown, list) and breakdown:
        first = breakdown[0]
        if isinstance(first, dict):
            return first.get("value", "Unknown")
        return first
    return "Unknown"

# Sums each bucket's breakdown series into per-category counts. A bucket with no
# point at all is marked as a gap and later classified as Unknown.
def aggregate_buckets(values: list, timestamps: list) -> tuple:
    series = []
    for entry in values:
        category = classify_label(breakdown_value(entry))
        time_map = {}
        for point in entry.get("dataPoints") or []:
            time_map[point["time"]] = point.get("value", 0)
        series.append((category, time_map))

    counts_list = []
    is_gap = []
    for ts in timestamps:
        counts = {}
        present = False
        for category, time_map in series:
            if ts in time_map:
                present = True
            amount = round(time_map.get(ts, 0))
            counts[category] = counts.get(category, 0) + amount
        counts_list.append(counts)
        is_gap.append(not present)
    return counts_list, is_gap

# Aggregates consecutive buckets into coarser chunks so the histogram never
# exceeds max_points characters.
def downsample_buckets(counts_list: list, timestamps: list, is_gap: list, max_points: int) -> tuple:
    if max_points <= 0 or len(timestamps) <= max_points:
        return counts_list, timestamps, is_gap

    chunk_size = (len(timestamps) + max_points - 1) // max_points
    new_counts = []
    new_timestamps = []
    new_gaps = []
    for start in range(0, len(timestamps), chunk_size):
        merged = {}
        for counts in counts_list[start:start + chunk_size]:
            for category, amount in counts.items():
                merged[category] = merged.get(category, 0) + amount
        new_counts.append(merged)
        new_timestamps.append(timestamps[start])
        new_gaps.append(all(is_gap[start:start + chunk_size]))
    return new_counts, new_timestamps, new_gaps

def classify_buckets(counts_list: list, is_gap: list, threshold: float) -> list:
    statuses = []
    for index, counts in enumerate(counts_list):
        if is_gap[index]:
            statuses.append("Unknown")
            continue
        total = sum(counts.values())
        failures = sum(counts.get(name, 0) for name in FAILURE_CATEGORIES)
        unknowns = counts.get("Unknown", 0)
        insufficients = counts.get("Insufficient", 0)
        if total <= 0:
            status = "Insufficient"
        elif unknowns > 0 and failures == 0:
            status = "Unknown"
        elif insufficients > 0 and failures == 0:
            status = "Insufficient"
        elif failures / total > threshold:
            status = "Critical"
        elif failures > 0:
            status = "Unhealthy"
        else:
            status = "Healthy"
        statuses.append(status)
    return statuses

def convert_time(time_value) -> datetime:
    if isinstance(time_value, (int, float)):
        if time_value > 10 ** 12:
            return datetime.fromtimestamp(time_value / 1000).astimezone()
        return datetime.fromtimestamp(time_value).astimezone()
    if isinstance(time_value, str):
        text = time_value.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).astimezone()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return datetime.now()
    return datetime.now()

def format_time(time_value) -> str:
    return convert_time(time_value).strftime("%Y-%m-%d %H:%M")

def glyph_pairs(statuses: list) -> list:
    return [(STATUS_GLYPH.get(status, "?"), STATUS_STYLE.get(status, "bold magenta")) for status in statuses]

def status_counts(statuses: list) -> dict:
    counts = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return counts

def build_snapshot(values: list, config: dict) -> dict:
    expected = expected_timestamps(config)
    counts_list, is_gap = aggregate_buckets(values, expected)
    if not expected:
        return {"NoData": True, "Counts": {}, "Statuses": [], "Glyphs": []}
    max_points = int(config.get("MaxPoints", 120))
    threshold = float(config.get("CriticalThreshold", 0.10))
    display_counts, display_times, display_gaps = downsample_buckets(counts_list, expected, is_gap, max_points)
    statuses = classify_buckets(display_counts, display_gaps, threshold)
    return {
        "Counts": status_counts(statuses),
        "Statuses": statuses,
        "Glyphs": glyph_pairs(statuses),
        "EarliestTime": format_time(display_times[0]) if display_times else None,
        "LatestTime": format_time(display_times[-1]) if display_times else None,
        "Timestamps": display_times,
        "RawTimestamps": expected,
        "RawCounts": counts_list,
        "RawIsGap": is_gap,
    }

# Maintains a fixed-size rolling window at raw bucket level. Each poll slides
# the window forward: every newly available bucket is appended and the same
# number of oldest buckets is hidden, so already rendered buckets are never
# rewritten. Downsampling is applied to the buffer at render time.
def update_rolling_history(rolling: dict, snapshot: dict) -> None:
    timestamps = snapshot.get("RawTimestamps") or []
    counts_list = snapshot.get("RawCounts") or []
    gaps = snapshot.get("RawIsGap") or []
    if not timestamps or len(counts_list) != len(timestamps):
        return
    window = len(timestamps)
    if rolling.get("Timestamps"):
        known_last = rolling["Timestamps"][-1]
        if known_last not in timestamps:
            rolling["Timestamps"] = list(timestamps)
            rolling["Counts"] = [dict(counts) for counts in counts_list]
            rolling["IsGap"] = list(gaps)
            return
        new_start = len(timestamps)
        for index, ts in enumerate(timestamps):
            if ts == known_last:
                new_start = index + 1
                break
        for index in range(new_start, len(timestamps)):
            rolling["Timestamps"].append(timestamps[index])
            rolling["Counts"].append(dict(counts_list[index]))
            rolling["IsGap"].append(gaps[index])
    else:
        rolling["Timestamps"] = list(timestamps)
        rolling["Counts"] = [dict(counts) for counts in counts_list]
        rolling["IsGap"] = list(gaps)
    overflow = len(rolling["Timestamps"]) - window
    if overflow > 0:
        del rolling["Timestamps"][:overflow]
        del rolling["Counts"][:overflow]
        del rolling["IsGap"][:overflow]

def build_display_snapshot(rolling: dict, template: dict, config: dict) -> dict:
    snapshot = dict(template)
    timestamps = rolling.get("Timestamps") or []
    counts_list = rolling.get("Counts") or []
    gaps = rolling.get("IsGap") or []
    if not timestamps:
        return snapshot
    max_points = int(config.get("MaxPoints", 120))
    threshold = float(config.get("CriticalThreshold", 0.10))
    counts_list, timestamps, gaps = downsample_buckets(counts_list, timestamps, gaps, max_points)
    statuses = classify_buckets(counts_list, gaps, threshold)
    snapshot["Counts"] = status_counts(statuses)
    snapshot["Statuses"] = statuses
    snapshot["Glyphs"] = glyph_pairs(statuses)
    snapshot["EarliestTime"] = format_time(timestamps[0])
    snapshot["LatestTime"] = format_time(timestamps[-1])
    return snapshot

def save_outputs(games: list, snapshots: dict) -> None:
    games_payload = {}
    for index, game in enumerate(games):
        snapshot = snapshots.get(index, {})
        if "Values" in snapshot:
            games_payload[str(game.get("UniverseId"))] = snapshot["Values"]
    payload = {
        "FetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Games": games_payload,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    history = []
    if HISTORY_PATH.exists():
        try:
            history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            history = []
    history.append(payload)
    HISTORY_PATH.write_text(json.dumps(history[-HISTORY_LIMIT:], indent=2), encoding="utf-8")

def shrink_glyphs(glyphs: list, max_width: int) -> list:
    if len(glyphs) <= max_width:
        return glyphs
    chunk_size = (len(glyphs) + max_width - 1) // max_width
    shrunk = []
    for start in range(0, len(glyphs), chunk_size):
        chunk = glyphs[start:start + chunk_size]
        worst = max(chunk, key=lambda item: GLYPH_RANK.get(item[0], 0))
        shrunk.append(worst)
    return shrunk

def build_game_section(name: str, snapshot: dict, width: int) -> Group:
    lines = [Text(name.upper(), style="bold white")]
    if "Error" in snapshot:
        lines.append(Text(f"ERROR: {snapshot['Error']}", style="bold red"))
        return Group(*lines)
    if snapshot.get("NoData"):
        lines.append(Text("No data for the selected time range", style="dim white"))
        lines.append(Text("Histogram", style="white"))
        lines.append(Text("No data", style="dim white"))
        return Group(*lines)

    snapshot.setdefault("LatestTime", "")
    snapshot.setdefault("EarliestTime", "")

    counts = snapshot["Counts"]
    line = Text()
    line.append("Healthy: ", style="white")
    line.append(str(counts.get("Healthy", 0)), style="bold green")
    line.append("  Unhealthy: ", style="white")
    line.append(str(counts.get("Unhealthy", 0)), style="bold yellow")
    line.append("  Critical: ", style="white")
    line.append(str(counts.get("Critical", 0)), style="bold red")
    if counts.get("Insufficient", 0):
        line.append("  Insufficient: ", style="white")
        line.append(str(counts["Insufficient"]), style="dim white")
    if counts.get("Unknown", 0):
        line.append("  Unknown: ", style="white")
        line.append(str(counts["Unknown"]), style="bold magenta")
    lines.append(line)
    lines.append(Text("Histogram", style="white"))

    latest_suffix = ""
    if snapshot["LatestTime"]:
        latest_suffix = f"{snapshot['LatestTime']} v"
    fit_width = max(20, width - len(latest_suffix) - 2)
    glyphs = shrink_glyphs(snapshot["Glyphs"], fit_width)

    if latest_suffix:
        # Pad so the marker sits directly above the final histogram glyph.
        pad = max(0, len(glyphs) - len(latest_suffix))
        lines.append(Text(" " * pad + latest_suffix, style="white"))
    histogram_line = Text()
    for glyph, style in glyphs:
        histogram_line.append(glyph, style=style)
    lines.append(histogram_line)
    if snapshot["EarliestTime"]:
        lines.append(Text(f"^ {snapshot['EarliestTime']}", style="white"))
    return Group(*lines)

def build_countdown(remaining: float, width: int, interval: float) -> Group:
    line = Text(f"Next check in {remaining:.1f} seconds ...", style="white")
    bar_width = max(width - 2, 10)
    elapsed_fraction = 1.0 - (remaining / interval) if interval > 0 else 0.0
    filled = max(0, min(bar_width, int(elapsed_fraction * bar_width)))
    bar = Text("#" * filled, style="dim white")
    return Group(line, bar)

def build_dashboard(games: list, snapshots: dict, remaining: float, width: int, interval: float) -> Group:
    rule = Text("─" * width, style="dim")
    sections: list[RenderableType] = [Text("DATASTORE MONITOR DASHBOARD", style="bold white"), rule]
    for index, game in enumerate(games):
        name = game.get("Name", f"GAME {index + 1}")
        snapshot = snapshots.get(index, {"Error": "No data yet"})
        sections.append(build_game_section(name, snapshot, width))
        if index < len(games) - 1:
            sections.append(rule)
    sections.append(rule)
    sections.append(build_countdown(remaining, width, interval))
    return Group(*sections)

def fetch_all_games(client: RobloxClient, config: dict, rolling_histories: dict | None = None) -> dict:
    games = config.get("Games", [])
    snapshots = {}
    for index, game in enumerate(games):
        universe_id = game.get("UniverseId")
        try:
            values = fetch_analytics(client, config, universe_id)
            snapshot = build_snapshot(values, config)
            snapshot["Values"] = values
            if rolling_histories is not None:
                rolling = rolling_histories.setdefault(index, {})
                update_rolling_history(rolling, snapshot)
                snapshot = build_display_snapshot(rolling, snapshot, config)
            snapshots[index] = snapshot
        except Exception as exc:
            snapshots[index] = {"Error": str(exc)}
    save_outputs(games, snapshots)
    return snapshots

def main() -> None:
    parser = argparse.ArgumentParser(description="Datastore Monitor Dashboard")
    parser.add_argument("--once", action="store_true", help="Fetch once, render, then exit")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to config.json")
    args = parser.parse_args()

    load_dotenv(ENV_PATH)
    cookie = os.environ.get("Cookie") or os.environ.get("ROBLOSECURITY")
    if not cookie:
        CONSOLE.print("[bold red]No cookie found.[/] Set Cookie=... in .env")
        sys.exit(1)

    config_path = Path(args.config)
    if not config_path.exists():
        CONSOLE.print(f"[bold red]Config not found:[/] {config_path}")
        sys.exit(1)
    config = load_config(config_path)

    games = config.get("Games", [])
    if not games:
        CONSOLE.print("[bold red]No games configured in config.json[/]")
        sys.exit(1)

    interval = float(config.get("PollSeconds", 30))
    width = CONSOLE.width

    client = RobloxClient(cookie)

    if args.once:
        snapshots = fetch_all_games(client, config)
        CONSOLE.print(build_dashboard(games, snapshots, 0.0, width, interval))
        return

    next_fetch = 0.0
    snapshots = {}
    rolling_histories = {}
    with Live(console=CONSOLE, refresh_per_second=4, screen=False) as live:
        while True:
            now = time.time()
            if now >= next_fetch:
                snapshots = fetch_all_games(client, config, rolling_histories)
                next_fetch = now + interval
            remaining = max(0.0, next_fetch - time.time())
            renderable = build_dashboard(games, snapshots, remaining, width, interval)
            live.update(renderable)
            time.sleep(0.25)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        CONSOLE.print("\n[dim]Dashboard stopped.[/]")
        sys.exit(0)
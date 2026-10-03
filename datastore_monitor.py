#!/usr/bin/env python3
"""
Datastore Monitor Dashboard
==========================
Terminal dashboard that tracks the health of your own Roblox game's
Data Store requests (Healthy / Unhealthy / Critical / Insufficient).

Uses the Creator Hub's internal analytics-query-gateway endpoint
(not Open Cloud), authenticated with your own .ROBLOSECURITY cookie.

Setup:
    1. pip install -r requirements.txt
    2. Put your cookie in .env as:  Cookie=...
    3. Edit config.json (games, granularity, interval)
    4. python datastore_monitor.py          # loop with countdown
       python datastore_monitor.py --once   # single fetch, then exit
"""

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
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
ENV_PATH = SCRIPT_DIR / ".env"
OUTPUT_PATH = SCRIPT_DIR / "output.json"
import re

HISTORY_PATH = SCRIPT_DIR / "output_history.json"

STATUS_ORDER = ["Healthy", "Unhealthy", "Critical", "Insufficient", "Unknown"]
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

# Wire enum values (verified): 2=OneMinute, 3=HalfHour, 4=OneDay, 5=OneWeek
GRANULARITY_ENUM = {"OneMinute": 2, "HalfHour": 3, "OneDay": 4, "OneWeek": 5}

Console_ = Console()


# ----------------------------------------------------------------------------
# Config / payload helpers
# ----------------------------------------------------------------------------

def LoadConfig(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def ComputeWindow(config: dict):
    end_time = datetime.now(timezone.utc)
    if config.get("HistoryHours"):
        start_time = end_time - timedelta(hours=float(config["HistoryHours"]))
    else:
        start_time = end_time - timedelta(days=config.get("HistoryDays", 7))
    return start_time, end_time


def GranularityMinutes(config: dict) -> int:
    return {
        "OneMinute": 1, "HalfHour": 30, "OneDay": 1440, "OneWeek": 10080,
    }.get(config.get("Granularity", "OneMinute"), 1)


def BuildExpectedTimestamps(config: dict) -> list:
    """Contiguous window of buckets a complete dataset must fill. Missing
    buckets (no point returned by the API) are treated as Unknown."""
    granule = GranularityMinutes(config)
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


def BuildQuery(config: dict, universe_id) -> dict:
    start_time, end_time = ComputeWindow(config)
    granularity_name = config.get("Granularity", "OneMinute")
    granularity_int = GRANULARITY_ENUM.get(granularity_name, 2)

    query = {
        "resourceType": "RESOURCE_TYPE_UNIVERSE",
        "resourceId": str(universe_id),
        "metric": config.get("Metric", "DatastoreRequestsByStatus"),
        "granularity": granularity_int,
        "startTime": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    breakdown = config.get("Breakdown", [{"dimensions": ["DatastoreStatus"]}])
    if breakdown:
        query["breakdown"] = breakdown
    return {"resourceType": "RESOURCE_TYPE_UNIVERSE", "resourceId": str(universe_id), "query": query}


def ExtractValues(data: dict) -> list:
    candidates = [
        ("operation", "queryResult", "values"),
        ("operation", "response", "values"),
        ("operation", "values"),
        ("queryResult", "values"),
        ("response", "values"),
        ("values",),
    ]
    for path_keys in candidates:
        current = data
        for key in path_keys:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if isinstance(current, list):
            return current
    return []


# ----------------------------------------------------------------------------
# Roblox client (cookie + CSRF session)
# ----------------------------------------------------------------------------

class RobloxClient:
    def __init__(self, cookie: str):
        self.Cookie = cookie
        self.CsrfToken = None
        self.Session = requests.Session()
        self.Session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/152.0.0.0 Safari/537.36"),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://create.roblox.com",
            "Referer": "https://create.roblox.com/",
        })
        self.Session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")

    def HarvestCsrfToken(self):
        # POST to /v2/logout without a token -> 403 -> header carries the token.
        try:
            response = self.Session.post(
                "https://auth.roblox.com/v2/logout", json={}, timeout=20
            )
            if response.status_code == 403:
                token = response.headers.get("x-csrf-token")
                if token:
                    return token
        except requests.RequestException:
            pass
        # Fallback: scrape the dashboard page for an embedded token.
        try:
            response = self.Session.get(
                "https://create.roblox.com/dashboard/creations", timeout=25
            )
            match = re.search(r'name="csrf-token"\s+content="([^"]+)"', response.text)
            if not match:
                match = re.search(r'["\']CsrfToken["\']\s*:\s*["\']([^"\']+)', response.text)
            if match:
                return match.group(1)
        except requests.RequestException:
            pass
        return None

    def EnsureCsrf(self):
        if not self.CsrfToken:
            self.CsrfToken = self.HarvestCsrfToken()

    def PostJson(self, url: str, payload: dict) -> requests.Response:
        self.EnsureCsrf()
        headers = {"x-csrf-token": self.CsrfToken} if self.CsrfToken else {}
        response = self.Session.post(url, json=payload, headers=headers, timeout=60)
        if response.status_code == 403:
            token = response.headers.get("x-csrf-token")
            if token:
                self.CsrfToken = token
            else:
                self.CsrfToken = None
                self.EnsureCsrf()
            headers = {"x-csrf-token": self.CsrfToken} if self.CsrfToken else {}
            response = self.Session.post(url, json=payload, headers=headers, timeout=60)
        return response

    def GetJson(self, url: str) -> requests.Response:
        headers = {"x-csrf-token": self.CsrfToken} if self.CsrfToken else {}
        return self.Session.get(url, headers=headers, timeout=60)


def ExtractErrorMessage(data, fallback: str) -> str:
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


def FetchAnalytics(client: RobloxClient, config: dict, universe_id) -> list:
    body = BuildQuery(config, universe_id)
    endpoint = config["Endpoint"].format(UniverseId=universe_id)

    response = client.PostJson(endpoint, body)
    data = None
    try:
        data = response.json()
    except Exception:
        data = None
    if response.status_code != 200:
        message = ExtractErrorMessage(data, f"HTTP {response.status_code}")
        raise RuntimeError(message)
    data = response.json()

    # Long-running queries: poll the returned operation path until done.
    operation = data.get("operation", data)
    attempts = 0
    while not operation.get("done", True) and operation.get("path") and attempts < 60:
        attempts += 1
        operation_id = operation["path"]
        poll_url = (
            "https://apis.roblox.com/analytics-query-gateway/v1/operations/"
            f"{operation_id}?resourceType=RESOURCE_TYPE_UNIVERSE&resourceId={universe_id}"
        )
        try:
            response = client.GetJson(poll_url)
            if response.status_code == 200:
                data = response.json()
                operation = data.get("operation", data)
        except requests.RequestException:
            pass
        time.sleep(1)

    if operation.get("error"):
        raise RuntimeError(f"Query error: {operation['error'].get('message', operation['error'])}")
    return ExtractValues(data)


# ----------------------------------------------------------------------------
# Aggregation (status breakdown -> per-timestamp classification)
# ----------------------------------------------------------------------------

def NormalizeLabel(label) -> str:
    return re.sub(r"[^a-z0-9]", "", str(label).lower())


KNOWN_HEALTHY_LABELS = {"ok", "200", "200ok", "success", "succeeded", "healthy", "200ok"}
KNOWN_INSUFFICIENT_LABELS = {
    "na", "none", "nodata", "notmeaningful",
    "notstatisticallysignificant", "insufficient",
}
KNOWN_FAILURE_LABELS = {
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
KNOWN_FAILURE_KEYWORDS = (
    "fail", "error", "throttl", "denied", "forbidden", "notfound",
    "invalid", "timeout", "unavailable", "toomany", "exceeded", "deleted",
    "limit", "empty", "larger", "lesser", "cannot", "cant", "mismatch",
    "notallowed", "nonnumeric", "transform", "keyname", "valuetoo",
)


def ClassifyStatusLabel(label) -> str:
    text = str(label)
    normalized = NormalizeLabel(text)
    if not normalized:
        return "Unknown"
    if normalized in KNOWN_HEALTHY_LABELS:
        return "Healthy"
    if normalized in KNOWN_INSUFFICIENT_LABELS or "insufficient" in normalized:
        return "Insufficient"
    if normalized in KNOWN_FAILURE_LABELS:
        return "Unhealthy"
    if any(keyword in normalized for keyword in KNOWN_FAILURE_KEYWORDS):
        return "Unhealthy"
    # Anything unrecognized: treat as Unknown (NOT healthy).
    return "Unknown"


def ExtractBreakdownValue(entry: dict):
    breakdown = entry.get("breakdownValue") or []
    if isinstance(breakdown, list) and breakdown:
        first = breakdown[0]
        if isinstance(first, dict):
            return first.get("value", "Unknown")
        return first
    return "Unknown"


def AggregateValues(values: list, sorted_timestamps: list):
    datasets = []
    for entry in values:
        status_value = ExtractBreakdownValue(entry)
        data_points = entry.get("dataPoints") or []
        dataset = {
            "Label": status_value,
            "Category": ClassifyStatusLabel(status_value),
            "Data": [],
            "TimeMap": {},
        }
        for point in data_points:
            time_key = point["time"]
            value = point.get("value", 0)
            dataset["TimeMap"][time_key] = value
        datasets.append(dataset)

    total_data = []
    is_gap = []
    for ts in sorted_timestamps:
        total = 0
        present = False
        for dataset in datasets:
            if ts in dataset["TimeMap"]:
                present = True
            value = round(dataset["TimeMap"].get(ts, 0))
            dataset["Data"].append(value)
            total += value
        total_data.append(total)
        is_gap.append(not present)
    return datasets, sorted_timestamps, total_data, is_gap


def Downsample(datasets: list, sorted_timestamps: list, total_data: list,
               is_gap: list, max_points: int):
    """Aggregate consecutive data points into coarser chunks so the histogram
    never exceeds `max_points` characters."""
    if max_points <= 0 or len(sorted_timestamps) <= max_points:
        return datasets, sorted_timestamps, total_data, is_gap

    chunk_size = (len(sorted_timestamps) + max_points - 1) // max_points
    new_datasets = [
        {"Label": d["Label"], "Category": d["Category"],
         "Data": [], "TimeMap": {}}
        for d in datasets
    ]
    new_timestamps = []
    new_total = []
    new_is_gap = []
    for start in range(0, len(sorted_timestamps), chunk_size):
        chunk = sorted_timestamps[start:start + chunk_size]
        new_timestamps.append(chunk[0])
        new_total.append(sum(total_data[start:start + chunk_size]))
        new_is_gap.append(all(is_gap[start:start + chunk_size]))
        for index, dataset in enumerate(datasets):
            new_datasets[index]["Data"].append(
                sum(dataset["Data"][start:start + chunk_size])
            )
    return new_datasets, new_timestamps, new_total, new_is_gap


def ClassifyTimestamps(datasets: list, sorted_timestamps: list,
                       is_gap: list, threshold: float) -> list:
    statuses = []
    for index in range(len(sorted_timestamps)):
        if is_gap[index]:
            statuses.append("Unknown")
            continue
        total = sum(dataset["Data"][index] for dataset in datasets)
        failures = sum(
            dataset["Data"][index] for dataset in datasets
            if dataset["Category"] in ("Unhealthy", "Critical")
        )
        unknowns = sum(
            dataset["Data"][index] for dataset in datasets
            if dataset["Category"] == "Unknown"
        )
        insufficients = sum(
            dataset["Data"][index] for dataset in datasets
            if dataset["Category"] == "Insufficient"
        )
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


def ConvertTime(time_value) -> datetime:
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


def FormatTime(time_value) -> str:
    return ConvertTime(time_value).strftime("%Y-%m-%d %H:%M")


def BuildSnapshot(values: list, config: dict) -> dict:
    expected = BuildExpectedTimestamps(config)
    datasets, sorted_timestamps, total_data, is_gap = AggregateValues(values, expected)
    if not sorted_timestamps:
        return {"NoData": True, "Counts": {}, "Statuses": [], "Glyphs": []}
    max_points = int(config.get("MaxPoints", 120))
    datasets, sorted_timestamps, total_data, is_gap = Downsample(
        datasets, sorted_timestamps, total_data, is_gap, max_points
    )
    threshold = float(config.get("CriticalThreshold", 0.10))
    statuses = ClassifyTimestamps(datasets, sorted_timestamps, is_gap, threshold)
    counts = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    glyphs = [(STATUS_GLYPH[status], STATUS_STYLE[status]) for status in statuses]
    return {
        "Counts": counts,
        "Statuses": statuses,
        "Glyphs": glyphs,
        "EarliestTime": FormatTime(sorted_timestamps[0]) if sorted_timestamps else None,
        "LatestTime": FormatTime(sorted_timestamps[-1]) if sorted_timestamps else None,
        "Datasets": datasets,
        "TotalData": total_data,
        "Timestamps": sorted_timestamps,
    }


def UpdateRollingHistory(rolling: dict, snapshot: dict) -> None:
    """Maintain a fixed-size rolling window for the histogram.

    On every poll the window slides one minute forward: append the status of
    every newly available minute and hide (drop) the matching number of the
    oldest values. Past minutes are kept as-is instead of being rewritten.
    """
    timestamps = snapshot.get("Timestamps") or []
    statuses = snapshot.get("Statuses") or []
    if not timestamps or not statuses:
        return
    window = len(timestamps)
    if rolling.get("Timestamps"):
        known_last = rolling["Timestamps"][-1]
        if known_last not in timestamps:
            rolling["Timestamps"] = list(timestamps)
            rolling["Statuses"] = list(statuses)
            return
        new_start = len(timestamps)
        for index, ts in enumerate(timestamps):
            if ts == known_last:
                new_start = index + 1
                break
        rolling["Timestamps"] += timestamps[new_start:]
        rolling["Statuses"] += statuses[new_start:]
    else:
        rolling["Timestamps"] = list(timestamps)
        rolling["Statuses"] = list(statuses)
    overflow = len(rolling["Timestamps"]) - window
    if overflow > 0:
        del rolling["Timestamps"][:overflow]
        del rolling["Statuses"][:overflow]


def BuildDisplaySnapshot(rolling: dict, template: dict) -> dict:
    """Derive the renderable snapshot from the rolling window buffer."""
    snapshot = dict(template)
    statuses = rolling.get("Statuses") or []
    timestamps = rolling.get("Timestamps") or []
    if not statuses:
        return snapshot
    counts = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    glyphs = [
        (STATUS_GLYPH.get(status, "?"), STATUS_STYLE.get(status, "bold magenta"))
        for status in statuses
    ]
    snapshot["Counts"] = counts
    snapshot["Statuses"] = statuses
    snapshot["Glyphs"] = glyphs
    snapshot["EarliestTime"] = FormatTime(timestamps[0])
    snapshot["LatestTime"] = FormatTime(timestamps[-1])
    return snapshot


# ----------------------------------------------------------------------------
# Persistence (debug output)
# ----------------------------------------------------------------------------

def SaveOutputs(games: list, snapshots: dict):
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
    HISTORY_PATH.write_text(json.dumps(history[-500:], indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# Rendering (mimics the mock dashboard layout)
# ----------------------------------------------------------------------------

def BuildHistogramLines(glyphs: list, width: int) -> list:
    if not glyphs:
        return [Text("No data", style="dim")]
    lines = []
    for start in range(0, len(glyphs), max(width, 10)):
        line = Text()
        for glyph, style in glyphs[start:start + max(width, 10)]:
            line.append(glyph, style=style)
        lines.append(line)
    return lines


SEVERITY_RANK = {"Critical": 3, "Unhealthy": 2, "Insufficient": 1, "Healthy": 0}


def ShrinkGlyphs(glyphs: list, max_width: int) -> list:
    """Ensure the histogram fits on one line; keep the worst status per chunk."""
    if len(glyphs) <= max_width:
        return glyphs
    chunk_size = (len(glyphs) + max_width - 1) // max_width
    shrunk = []
    for start in range(0, len(glyphs), chunk_size):
        chunk = glyphs[start:start + chunk_size]
        worst = max(chunk, key=lambda item: SEVERITY_RANK.get(item[0], 0))
        shrunk.append(worst)
    return shrunk


def BuildGameSection(name: str, snapshot: dict, width: int) -> Group:
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
    glyphs = ShrinkGlyphs(snapshot["Glyphs"], fit_width)

    if latest_suffix:
        # Pad so the "v" sits directly above the final histogram glyph.
        pad = max(0, len(glyphs) - len(latest_suffix))
        lines.append(Text(" " * pad + latest_suffix, style="white"))
    histogram_line = Text()
    for glyph, style in glyphs:
        histogram_line.append(glyph, style=style)
    lines.append(histogram_line)
    if snapshot["EarliestTime"]:
        lines.append(Text(f"^ {snapshot['EarliestTime']}", style="white"))
    return Group(*lines)


def BuildCountdown(remaining: float, width: int, interval: float) -> Group:
    line = Text(f"Next check in {remaining:.1f} seconds ...", style="white")
    bar_width = max(width - 2, 10)
    elapsed_fraction = 1.0 - (remaining / interval) if interval > 0 else 0.0
    filled = max(0, min(bar_width, int(elapsed_fraction * bar_width)))
    bar = Text("#" * filled, style="dim white")
    return Group(line, bar)


def BuildDashboard(games: list, snapshots: dict, remaining: float, width: int,
                   interval: float) -> Group:
    rule = Text("─" * width, style="dim")
    sections = [Text("DATASTORE MONITOR DASHBOARD", style="bold white"), rule]
    for index, game in enumerate(games):
        name = game.get("Name", f"GAME {index + 1}")
        snapshot = snapshots.get(index, {"Error": "No data yet"})
        sections.append(BuildGameSection(name, snapshot, width)) # type: ignore
        if index < len(games) - 1:
            sections.append(rule)
    sections.append(rule)
    sections.append(BuildCountdown(remaining, width, interval)) # type: ignore
    return Group(*sections)


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------

def FetchAllGames(client: RobloxClient, config: dict,
                  rolling_histories: dict = None) -> dict:
    games = config.get("Games", [])
    snapshots = {}
    for index, game in enumerate(games):
        universe_id = game.get("UniverseId")
        try:
            values = FetchAnalytics(client, config, universe_id)
            snapshot = BuildSnapshot(values, config)
            snapshot["Values"] = values
            if rolling_histories is not None:
                rolling = rolling_histories.setdefault(index, {})
                UpdateRollingHistory(rolling, snapshot)
                snapshot = BuildDisplaySnapshot(rolling, snapshot)
            snapshots[index] = snapshot
        except Exception as exc:
            snapshots[index] = {"Error": str(exc)}
    SaveOutputs(games, snapshots)
    return snapshots


def Main():
    parser = argparse.ArgumentParser(description="Datastore Monitor Dashboard")
    parser.add_argument("--once", action="store_true",
                        help="Fetch once, render, then exit")
    parser.add_argument("--config", default=str(CONFIG_PATH),
                        help="Path to config.json")
    args = parser.parse_args()

    load_dotenv(ENV_PATH)
    cookie = os.environ.get("Cookie") or os.environ.get("ROBLOSECURITY")
    if not cookie:
        Console_.print("[bold red]No cookie found.[/] Set Cookie=... in .env")
        sys.exit(1)

    config_path = Path(args.config)
    if not config_path.exists():
        Console_.print(f"[bold red]Config not found:[/] {config_path}")
        sys.exit(1)
    config = LoadConfig(config_path)

    games = config.get("Games", [])
    if not games:
        Console_.print("[bold red]No games configured in config.json[/]")
        sys.exit(1)

    interval = float(config.get("PollSeconds", 30))
    width = Console_.width

    client = RobloxClient(cookie)

    if args.once:
        snapshots = FetchAllGames(client, config)
        Console_.print(BuildDashboard(games, snapshots, 0.0, width, interval))
        return

    next_fetch = 0.0
    snapshots = {}
    rolling_histories = {}
    with Live(console=Console_, refresh_per_second=4, screen=False) as live:
        while True:
            now = time.time()
            if now >= next_fetch:
                snapshots = FetchAllGames(client, config, rolling_histories)
                next_fetch = now + interval
            remaining = max(0.0, next_fetch - time.time())
            renderable = BuildDashboard(games, snapshots, remaining, width, interval)
            live.update(renderable)
            time.sleep(0.25)


if __name__ == "__main__":
    try:
        Main()
    except KeyboardInterrupt:
        Console_.print("\n[dim]Dashboard stopped.[/]")
        sys.exit(0)